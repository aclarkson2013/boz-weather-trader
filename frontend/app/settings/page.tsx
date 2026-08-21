"use client";

import { FileText, Loader2, Save, Settings } from "lucide-react";
import { useEffect, useState } from "react";
import { mutate } from "swr";

import LogsViewer from "@/components/logs/logs-viewer";
import ErrorBoundary from "@/components/ui/error-boundary";
import Skeleton from "@/components/ui/skeleton";
import WeatherTicker from "@/components/weather-ticker/weather-ticker";
import { disconnect, fetchUpdateStatus, triggerUpdate, updateSettings } from "@/lib/api";
import { useAuthStatus, useSettings, useVersion } from "@/lib/hooks";
import type {
  CityCode,
  SettingsUpdate,
  TradingMode,
  UpdateStatus,
  WeatherSourceName,
} from "@/lib/types";
import { centsToDollars } from "@/lib/utils";

const ALL_CITIES: CityCode[] = ["NYC", "CHI", "MIA", "AUS"];

const ALL_WEATHER_SOURCES: WeatherSourceName[] = [
  "NWS",
  "NWS:gridpoint",
  "Open-Meteo:ECMWF",
  "Open-Meteo:GFS",
  "Open-Meteo:ICON",
];

// Short notes shown under each toggle, from the 2026-08-21 source review.
const WEATHER_SOURCE_NOTES: Record<WeatherSourceName, string> = {
  "NWS": "Near-duplicate of NWS:gridpoint (98% correlated)",
  "NWS:gridpoint": "Best NWS feed — MAE 2.41°F",
  "Open-Meteo:ECMWF": "Weakest member — MAE 3.25°F",
  "Open-Meteo:GFS": "Adds independent signal — MAE 2.67°F",
  "Open-Meteo:ICON": "Only source that measurably matters (p=0.004)",
};

// The ensemble needs a spread; matches MIN_ENABLED_WEATHER_SOURCES on the API.
const MIN_WEATHER_SOURCES = 2;

type SettingsTab = "settings" | "logs";

export default function SettingsPage() {
  const [activeTab, setActiveTab] = useState<SettingsTab>("settings");

  const { data: settings, error, isLoading } = useSettings();
  const { data: authStatus } = useAuthStatus();
  const { data: versionInfo } = useVersion();

  // Local form state
  const [tradingMode, setTradingMode] = useState<TradingMode>("manual");
  const [maxTradeSize, setMaxTradeSize] = useState(100);
  const [dailyLossLimit, setDailyLossLimit] = useState(1000);
  const [maxExposure, setMaxExposure] = useState(2500);
  const [minEv, setMinEv] = useState(0.05);
  const [minEvYes, setMinEvYes] = useState(0.15);
  const [minEvNo, setMinEvNo] = useState(0.05);
  const [cooldown, setCooldown] = useState(60);
  const [consecutiveLossLimit, setConsecutiveLossLimit] = useState(3);
  const [enableConsecutiveLossLimit, setEnableConsecutiveLossLimit] = useState(true);
  const [enablePerLossCooldown, setEnablePerLossCooldown] = useState(true);
  const [maxContractsPerBracket, setMaxContractsPerBracket] = useState(3);
  const [activeCities, setActiveCities] = useState<CityCode[]>(ALL_CITIES);
  const [enabledSources, setEnabledSources] = useState<WeatherSourceName[]>([
    "NWS:gridpoint",
    "Open-Meteo:GFS",
    "Open-Meteo:ICON",
  ]);
  const [notifications, setNotifications] = useState(true);

  // Fee estimation
  const [feeEstimateMode, setFeeEstimateMode] = useState("conservative");

  // Model guardrails
  const [modelWeight, setModelWeight] = useState(0.4);
  const [maxDivergence, setMaxDivergence] = useState(0.25);
  const [minMarketProb, setMinMarketProb] = useState(0.15);

  // Display preferences
  const [timezone, setTimezone] = useState("");

  const [saving, setSaving] = useState(false);
  const [saveMessage, setSaveMessage] = useState<string | null>(null);
  const [disconnecting, setDisconnecting] = useState(false);

  // Self-update state
  const [updateStatus, setUpdateStatus] = useState<UpdateStatus | null>(null);
  const [updatePolling, setUpdatePolling] = useState(false);

  // Sync local state when settings load
  useEffect(() => {
    if (settings) {
      setTradingMode(settings.trading_mode);
      setMaxTradeSize(settings.max_trade_size_cents);
      setDailyLossLimit(settings.daily_loss_limit_cents);
      setMaxExposure(settings.max_daily_exposure_cents);
      setMinEv(settings.min_ev_threshold);
      setMinEvYes(settings.min_ev_threshold_yes);
      setMinEvNo(settings.min_ev_threshold_no);
      setCooldown(settings.cooldown_per_loss_minutes);
      setConsecutiveLossLimit(settings.consecutive_loss_limit);
      setEnableConsecutiveLossLimit(settings.enable_consecutive_loss_limit);
      setEnablePerLossCooldown(settings.enable_per_loss_cooldown);
      setMaxContractsPerBracket(settings.max_contracts_per_bracket);
      setActiveCities(settings.active_cities);
      if (settings.enabled_weather_sources) {
        setEnabledSources(settings.enabled_weather_sources);
      }
      setNotifications(settings.notifications_enabled);
      setModelWeight(settings.model_weight);
      setMaxDivergence(settings.max_model_market_divergence);
      setMinMarketProb(settings.min_market_prob_for_yes);
      setFeeEstimateMode(settings.fee_estimate_mode || "conservative");
      setTimezone(settings.timezone);
    }
  }, [settings]);

  const toggleCity = (city: CityCode) => {
    setActiveCities((prev) =>
      prev.includes(city)
        ? prev.filter((c) => c !== city)
        : [...prev, city]
    );
  };

  const toggleWeatherSource = (source: WeatherSourceName) => {
    setEnabledSources((prev) => {
      if (!prev.includes(source)) return [...prev, source];
      // Never let the user drop below the ensemble minimum.
      if (prev.length <= MIN_WEATHER_SOURCES) return prev;
      return prev.filter((s) => s !== source);
    });
  };

  const handleSave = async () => {
    setSaving(true);
    setSaveMessage(null);
    try {
      const update: SettingsUpdate = {
        trading_mode: tradingMode,
        max_trade_size_cents: maxTradeSize,
        daily_loss_limit_cents: dailyLossLimit,
        max_daily_exposure_cents: maxExposure,
        min_ev_threshold: minEv,
        min_ev_threshold_yes: minEvYes,
        min_ev_threshold_no: minEvNo,
        cooldown_per_loss_minutes: cooldown,
        consecutive_loss_limit: consecutiveLossLimit,
        enable_consecutive_loss_limit: enableConsecutiveLossLimit,
        enable_per_loss_cooldown: enablePerLossCooldown,
        max_contracts_per_bracket: maxContractsPerBracket,
        active_cities: activeCities,
        enabled_weather_sources: enabledSources,
        notifications_enabled: notifications,
        model_weight: modelWeight,
        max_model_market_divergence: maxDivergence,
        min_market_prob_for_yes: minMarketProb,
        fee_estimate_mode: feeEstimateMode,
        timezone,
      };
      await updateSettings(update);
      await mutate("/api/settings");
      setSaveMessage("Settings saved!");
      setTimeout(() => setSaveMessage(null), 3000);
    } catch (err) {
      setSaveMessage(
        err instanceof Error ? err.message : "Failed to save settings"
      );
    } finally {
      setSaving(false);
    }
  };

  // Poll update status while update is in progress
  useEffect(() => {
    if (!updatePolling) return;
    const interval = setInterval(async () => {
      try {
        const status = await fetchUpdateStatus();
        setUpdateStatus(status);
        if (status.status === "done" || status.status === "error" || status.status === "idle") {
          setUpdatePolling(false);
        }
      } catch {
        // Backend might be restarting — keep polling
      }
    }, 3000);
    return () => clearInterval(interval);
  }, [updatePolling]);

  const handleUpdate = async () => {
    if (!confirm("This will pull the latest code, rebuild, and restart all containers. Continue?")) {
      return;
    }
    try {
      const result = await triggerUpdate();
      if (result.status === "started" || result.status === "already_running") {
        setUpdatePolling(true);
        setUpdateStatus({ status: "pulling", step: "starting", error: null, started_at: null });
      } else {
        setUpdateStatus({
          status: "error",
          step: null,
          error: result.message,
          started_at: null,
        });
      }
    } catch (err) {
      setUpdateStatus({
        status: "error",
        step: null,
        error: err instanceof Error ? err.message : "Failed to start update",
        started_at: null,
      });
    }
  };

  const handleDisconnect = async () => {
    if (!confirm("Are you sure you want to disconnect your Kalshi account? This will delete all your data.")) {
      return;
    }
    setDisconnecting(true);
    try {
      await disconnect();
      window.location.href = "/onboarding";
    } catch (err) {
      alert(err instanceof Error ? err.message : "Disconnect failed");
      setDisconnecting(false);
    }
  };

  return (
    <ErrorBoundary>
      {/* Header with tab toggle */}
      <div className="flex items-center justify-between mb-4">
        <h1 className="text-xl font-bold">Settings</h1>
        <div className="flex bg-gray-100 rounded-lg p-0.5">
          <button
            onClick={() => setActiveTab("settings")}
            className={`flex items-center gap-1.5 px-3 py-1.5 rounded-md text-xs font-medium transition-colors ${
              activeTab === "settings"
                ? "bg-white text-boz-primary shadow-sm"
                : "text-boz-neutral hover:text-gray-900"
            }`}
          >
            <Settings size={14} />
            Settings
          </button>
          <button
            onClick={() => setActiveTab("logs")}
            className={`flex items-center gap-1.5 px-3 py-1.5 rounded-md text-xs font-medium transition-colors ${
              activeTab === "logs"
                ? "bg-white text-boz-primary shadow-sm"
                : "text-boz-neutral hover:text-gray-900"
            }`}
          >
            <FileText size={14} />
            Logs
          </button>
        </div>
      </div>
      <WeatherTicker />

      {/* Tab content */}
      {activeTab === "settings" ? (
        <>
          {/* Settings loading state */}
          {isLoading && (
            <div className="space-y-4">
              {[...Array(6)].map((_, i) => (
                <Skeleton key={i} className="h-16" />
              ))}
            </div>
          )}

          {/* Settings error state */}
          {error && (
            <div className="bg-red-50 border border-red-200 rounded-lg p-4 text-sm text-boz-danger">
              {error.message || "Unable to load settings"}
            </div>
          )}

          {/* Settings content */}
          {!isLoading && !error && (
            <div className="space-y-6">
              {/* Connection Status */}
              {authStatus && (
                <section className="bg-white rounded-lg border border-gray-200 shadow-sm p-4">
                  <h2 className="text-sm font-semibold mb-3">Connection Status</h2>
                  <div className="flex items-center justify-between">
                    <div className="flex items-center gap-2">
                      <span className="w-2.5 h-2.5 rounded-full bg-boz-success" />
                      <span className="text-sm font-medium">Connected</span>
                    </div>
                    <span
                      className={`inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium ${
                        authStatus.demo_mode
                          ? "bg-orange-100 text-orange-700"
                          : "bg-green-100 text-green-700"
                      }`}
                    >
                      {authStatus.demo_mode ? "DEMO" : "LIVE"}
                    </span>
                  </div>
                  <p className="text-xs text-boz-neutral mt-2">
                    Key: {authStatus.key_id_prefix}
                  </p>
                </section>
              )}

              {/* Trading Mode */}
              <section className="bg-white rounded-lg border border-gray-200 shadow-sm p-4">
                <h2 className="text-sm font-semibold mb-3">Trading Mode</h2>
                <div className="flex gap-2">
                  {(["manual", "auto"] as TradingMode[]).map((mode) => (
                    <button
                      key={mode}
                      onClick={() => setTradingMode(mode)}
                      className={`min-h-[44px] flex-1 px-4 py-2 rounded-lg text-sm font-medium capitalize transition-colors ${
                        tradingMode === mode
                          ? "bg-boz-primary text-white"
                          : "bg-gray-100 text-boz-neutral hover:bg-gray-200"
                      }`}
                    >
                      {mode}
                    </button>
                  ))}
                </div>
                <p className="text-xs text-boz-neutral mt-2">
                  {tradingMode === "auto"
                    ? "Trades are executed automatically when +EV opportunities are found."
                    : "Trades require your approval in the Queue before execution."}
                </p>
              </section>

              {/* Risk Limits */}
              <section className="bg-white rounded-lg border border-gray-200 shadow-sm p-4">
                <h2 className="text-sm font-semibold mb-3">Risk Limits</h2>
                <div className="space-y-4">
                  <div>
                    <label className="flex justify-between text-xs mb-1">
                      <span>Max Trade Size</span>
                      <span className="font-medium">${centsToDollars(maxTradeSize)}</span>
                    </label>
                    <input
                      type="range"
                      min={10}
                      max={1000}
                      step={10}
                      value={maxTradeSize}
                      onChange={(e) => setMaxTradeSize(Number(e.target.value))}
                      className="w-full h-2 bg-gray-200 rounded-lg appearance-none cursor-pointer accent-boz-primary"
                    />
                  </div>
                  <div>
                    <label className="flex justify-between text-xs mb-1">
                      <span>Daily Loss Limit</span>
                      <span className="font-medium">${centsToDollars(dailyLossLimit)}</span>
                    </label>
                    <input
                      type="range"
                      min={100}
                      max={10000}
                      step={100}
                      value={dailyLossLimit}
                      onChange={(e) => setDailyLossLimit(Number(e.target.value))}
                      className="w-full h-2 bg-gray-200 rounded-lg appearance-none cursor-pointer accent-boz-primary"
                    />
                  </div>
                  <div>
                    <label className="flex justify-between text-xs mb-1">
                      <span>Max Daily Exposure</span>
                      <span className="font-medium">${centsToDollars(maxExposure)}</span>
                    </label>
                    <input
                      type="range"
                      min={100}
                      max={25000}
                      step={100}
                      value={maxExposure}
                      onChange={(e) => setMaxExposure(Number(e.target.value))}
                      className="w-full h-2 bg-gray-200 rounded-lg appearance-none cursor-pointer accent-boz-primary"
                    />
                  </div>
                  <div>
                    <label className="flex justify-between text-xs mb-1">
                      <span>Min EV (YES)</span>
                      <span className="font-medium">{(minEvYes * 100).toFixed(0)}%</span>
                    </label>
                    <input
                      type="range"
                      min={0.01}
                      max={0.5}
                      step={0.01}
                      value={minEvYes}
                      onChange={(e) => setMinEvYes(Number(e.target.value))}
                      className="w-full h-2 bg-gray-200 rounded-lg appearance-none cursor-pointer accent-boz-primary"
                      data-testid="min-ev-yes-slider"
                    />
                    <p className="text-xs text-boz-neutral mt-1">
                      Minimum expected value to place a YES-side trade
                    </p>
                  </div>
                  <div>
                    <label className="flex justify-between text-xs mb-1">
                      <span>Min EV (NO)</span>
                      <span className="font-medium">{(minEvNo * 100).toFixed(0)}%</span>
                    </label>
                    <input
                      type="range"
                      min={0.01}
                      max={0.5}
                      step={0.01}
                      value={minEvNo}
                      onChange={(e) => setMinEvNo(Number(e.target.value))}
                      className="w-full h-2 bg-gray-200 rounded-lg appearance-none cursor-pointer accent-boz-primary"
                      data-testid="min-ev-no-slider"
                    />
                    <p className="text-xs text-boz-neutral mt-1">
                      Minimum expected value to place a NO-side trade
                    </p>
                  </div>
                  <div>
                    <label className="flex justify-between text-xs mb-1">
                      <span className="flex items-center gap-2">
                        <button
                          onClick={() => setEnablePerLossCooldown(!enablePerLossCooldown)}
                          className={`relative w-8 h-5 rounded-full transition-colors flex-shrink-0 ${
                            enablePerLossCooldown ? "bg-boz-primary" : "bg-gray-300"
                          }`}
                          data-testid="per-loss-cooldown-toggle"
                        >
                          <span
                            className={`absolute top-0.5 left-0.5 w-4 h-4 bg-white rounded-full transition-transform shadow ${
                              enablePerLossCooldown ? "translate-x-3" : "translate-x-0"
                            }`}
                          />
                        </button>
                        Cooldown After Loss
                      </span>
                      <span className="font-medium">
                        {enablePerLossCooldown ? `${cooldown} min` : "Off"}
                      </span>
                    </label>
                    <input
                      type="range"
                      min={0}
                      max={1440}
                      step={15}
                      value={cooldown}
                      onChange={(e) => setCooldown(Number(e.target.value))}
                      disabled={!enablePerLossCooldown}
                      className={`w-full h-2 bg-gray-200 rounded-lg appearance-none cursor-pointer accent-boz-primary ${
                        !enablePerLossCooldown ? "opacity-40 cursor-not-allowed" : ""
                      }`}
                    />
                    <p className="text-xs text-boz-neutral mt-1">
                      {enablePerLossCooldown
                        ? `Pauses trading for ${cooldown} minutes after each loss`
                        : "Per-loss cooldown disabled"}
                    </p>
                  </div>
                  <div>
                    <label className="flex justify-between text-xs mb-1">
                      <span className="flex items-center gap-2">
                        <button
                          onClick={() => setEnableConsecutiveLossLimit(!enableConsecutiveLossLimit)}
                          className={`relative w-8 h-5 rounded-full transition-colors flex-shrink-0 ${
                            enableConsecutiveLossLimit ? "bg-boz-primary" : "bg-gray-300"
                          }`}
                          data-testid="consecutive-loss-toggle"
                        >
                          <span
                            className={`absolute top-0.5 left-0.5 w-4 h-4 bg-white rounded-full transition-transform shadow ${
                              enableConsecutiveLossLimit ? "translate-x-3" : "translate-x-0"
                            }`}
                          />
                        </button>
                        Consecutive Loss Limit
                      </span>
                      <span className="font-medium">
                        {enableConsecutiveLossLimit ? consecutiveLossLimit : "Off"}
                      </span>
                    </label>
                    <input
                      type="range"
                      min={1}
                      max={10}
                      step={1}
                      value={consecutiveLossLimit}
                      onChange={(e) => setConsecutiveLossLimit(Number(e.target.value))}
                      disabled={!enableConsecutiveLossLimit}
                      className={`w-full h-2 bg-gray-200 rounded-lg appearance-none cursor-pointer accent-boz-primary ${
                        !enableConsecutiveLossLimit ? "opacity-40 cursor-not-allowed" : ""
                      }`}
                    />
                    <p className="text-xs text-boz-neutral mt-1">
                      {enableConsecutiveLossLimit
                        ? `Pauses trading for the rest of the day after ${consecutiveLossLimit} consecutive losses`
                        : "Rest-of-day cooldown disabled — per-loss cooldown still applies"}
                    </p>
                  </div>
                  <div>
                    <label className="flex justify-between text-xs mb-1">
                      <span>Max Contracts Per Bracket</span>
                      <span className="font-medium">{maxContractsPerBracket}</span>
                    </label>
                    <input
                      type="range"
                      min={1}
                      max={20}
                      step={1}
                      value={maxContractsPerBracket}
                      onChange={(e) => setMaxContractsPerBracket(Number(e.target.value))}
                      className="w-full h-2 bg-gray-200 rounded-lg appearance-none cursor-pointer accent-boz-primary"
                      data-testid="bracket-cap-slider"
                    />
                    <p className="text-xs text-boz-neutral mt-1">
                      Hard cap on open contracts per bracket per market — prevents buying the same bracket repeatedly
                    </p>
                  </div>
                </div>
              </section>

              {/* Fee Estimation */}
              <section className="bg-white rounded-lg border border-gray-200 shadow-sm p-4">
                <h2 className="text-sm font-semibold mb-3">Fee Estimation</h2>
                <div className="flex gap-2">
                  {(["conservative", "realistic"] as const).map((mode) => (
                    <button
                      key={mode}
                      onClick={() => setFeeEstimateMode(mode)}
                      className={`min-h-[44px] flex-1 px-4 py-2 rounded-lg text-sm font-medium capitalize transition-colors ${
                        feeEstimateMode === mode
                          ? "bg-boz-primary text-white"
                          : "bg-gray-100 text-boz-neutral hover:bg-gray-200"
                      }`}
                      data-testid={`fee-mode-${mode}`}
                    >
                      {mode}
                    </button>
                  ))}
                </div>
                <p className="text-xs text-boz-neutral mt-2">
                  Realistic accounts for most limit orders having zero fees
                </p>
              </section>

              {/* Model Guardrails */}
              <section className="bg-white rounded-lg border border-gray-200 shadow-sm p-4">
                <h2 className="text-sm font-semibold mb-1">Model Guardrails</h2>
                <p className="text-xs text-boz-neutral mb-3">
                  Prevent the model from overriding market consensus on trade decisions.
                </p>
                <div className="space-y-4">
                  <div>
                    <label className="flex justify-between text-xs mb-1">
                      <span>Model Weight in Blend</span>
                      <span className="font-medium">{(modelWeight * 100).toFixed(0)}%</span>
                    </label>
                    <input
                      type="range"
                      min={0}
                      max={1}
                      step={0.05}
                      value={modelWeight}
                      onChange={(e) => setModelWeight(Number(e.target.value))}
                      className="w-full h-2 bg-gray-200 rounded-lg appearance-none cursor-pointer accent-boz-primary"
                      data-testid="model-weight-slider"
                    />
                    <p className="text-xs text-boz-neutral mt-1">
                      {(modelWeight * 100).toFixed(0)}% model / {((1 - modelWeight) * 100).toFixed(0)}% market.
                      Lower = more trust in market prices.
                    </p>
                  </div>
                  <div>
                    <label className="flex justify-between text-xs mb-1">
                      <span>Max Model-Market Divergence</span>
                      <span className="font-medium">{(maxDivergence * 100).toFixed(0)}%</span>
                    </label>
                    <input
                      type="range"
                      min={0.05}
                      max={0.50}
                      step={0.05}
                      value={maxDivergence}
                      onChange={(e) => setMaxDivergence(Number(e.target.value))}
                      className="w-full h-2 bg-gray-200 rounded-lg appearance-none cursor-pointer accent-boz-primary"
                      data-testid="max-divergence-slider"
                    />
                    <p className="text-xs text-boz-neutral mt-1">
                      Clamps model probability within &plusmn;{(maxDivergence * 100).toFixed(0)}% of market price before blending
                    </p>
                  </div>
                  <div>
                    <label className="flex justify-between text-xs mb-1">
                      <span>Min Market Prob for YES</span>
                      <span className="font-medium">{(minMarketProb * 100).toFixed(0)}%</span>
                    </label>
                    <input
                      type="range"
                      min={0}
                      max={0.50}
                      step={0.05}
                      value={minMarketProb}
                      onChange={(e) => setMinMarketProb(Number(e.target.value))}
                      className="w-full h-2 bg-gray-200 rounded-lg appearance-none cursor-pointer accent-boz-primary"
                      data-testid="min-market-prob-slider"
                    />
                    <p className="text-xs text-boz-neutral mt-1">
                      Skip YES trades on brackets the market prices below {(minMarketProb * 100).toFixed(0)}%
                    </p>
                  </div>
                </div>
              </section>

              {/* Active Cities */}
              <section className="bg-white rounded-lg border border-gray-200 shadow-sm p-4">
                <h2 className="text-sm font-semibold mb-3">Active Cities</h2>
                <div className="grid grid-cols-2 gap-2">
                  {ALL_CITIES.map((city) => (
                    <button
                      key={city}
                      onClick={() => toggleCity(city)}
                      className={`min-h-[44px] px-4 py-2 rounded-lg text-sm font-medium transition-colors ${
                        activeCities.includes(city)
                          ? "bg-boz-primary text-white"
                          : "bg-gray-100 text-boz-neutral hover:bg-gray-200"
                      }`}
                    >
                      {city}
                    </button>
                  ))}
                </div>
              </section>

              {/* Weather Sources */}
              <section className="bg-white rounded-lg border border-gray-200 shadow-sm p-4">
                <h2 className="text-sm font-semibold mb-1">Weather Sources</h2>
                <p className="text-xs text-boz-neutral mb-3">
                  Which forecast feeds are blended into the prediction ensemble.
                  Disabled sources are still fetched and scored, so you can turn
                  them back on at any time.
                </p>
                <div className="space-y-2">
                  {ALL_WEATHER_SOURCES.map((source) => {
                    const enabled = enabledSources.includes(source);
                    const atMinimum =
                      enabled && enabledSources.length <= MIN_WEATHER_SOURCES;
                    return (
                      <button
                        key={source}
                        onClick={() => toggleWeatherSource(source)}
                        disabled={atMinimum}
                        data-testid={`weather-source-${source}`}
                        aria-pressed={enabled}
                        className={`w-full min-h-[44px] px-4 py-2 rounded-lg text-left transition-colors ${
                          enabled
                            ? "bg-boz-primary text-white"
                            : "bg-gray-100 text-boz-neutral hover:bg-gray-200"
                        } ${atMinimum ? "opacity-70 cursor-not-allowed" : ""}`}
                        title={
                          atMinimum
                            ? `At least ${MIN_WEATHER_SOURCES} sources must stay enabled`
                            : undefined
                        }
                      >
                        <span className="block text-sm font-medium">{source}</span>
                        <span
                          className={`block text-xs ${
                            enabled ? "text-white/80" : "text-boz-neutral"
                          }`}
                        >
                          {WEATHER_SOURCE_NOTES[source]}
                        </span>
                      </button>
                    );
                  })}
                </div>
                <p className="text-xs text-boz-neutral mt-3">
                  {enabledSources.length} of {ALL_WEATHER_SOURCES.length} enabled
                  {enabledSources.length <= MIN_WEATHER_SOURCES &&
                    ` — minimum is ${MIN_WEATHER_SOURCES}`}
                </p>
              </section>

              {/* Display Preferences */}
              <section className="bg-white rounded-lg border border-gray-200 shadow-sm p-4">
                <h2 className="text-sm font-semibold mb-3">Display</h2>
                <div>
                  <label className="block text-xs mb-1">Timezone</label>
                  <select
                    value={timezone}
                    onChange={(e) => setTimezone(e.target.value)}
                    className="w-full min-h-[44px] px-3 py-2 bg-gray-50 border border-gray-200 rounded-lg text-sm appearance-none cursor-pointer focus:outline-none focus:ring-2 focus:ring-boz-primary focus:border-transparent"
                    data-testid="timezone-select"
                  >
                    <option value="">Browser Default</option>
                    <option value="America/New_York">Eastern (ET)</option>
                    <option value="America/Chicago">Central (CT)</option>
                    <option value="America/Denver">Mountain (MT)</option>
                    <option value="America/Los_Angeles">Pacific (PT)</option>
                    <option value="America/Anchorage">Alaska (AKT)</option>
                    <option value="Pacific/Honolulu">Hawaii (HT)</option>
                    <option value="UTC">UTC</option>
                  </select>
                  <p className="text-xs text-boz-neutral mt-1">
                    Controls how timestamps are displayed across the app
                  </p>
                </div>
              </section>

              {/* Notifications */}
              <section className="bg-white rounded-lg border border-gray-200 shadow-sm p-4">
                <div className="flex items-center justify-between">
                  <div>
                    <h2 className="text-sm font-semibold">Notifications</h2>
                    <p className="text-xs text-boz-neutral">
                      Receive push alerts for trades and settlements
                    </p>
                  </div>
                  <button
                    onClick={() => setNotifications(!notifications)}
                    className={`relative w-12 h-7 rounded-full transition-colors ${
                      notifications ? "bg-boz-primary" : "bg-gray-300"
                    }`}
                  >
                    <span
                      className={`absolute top-0.5 left-0.5 w-6 h-6 bg-white rounded-full transition-transform shadow ${
                        notifications ? "translate-x-5" : "translate-x-0"
                      }`}
                    />
                  </button>
                </div>
              </section>

              {/* Save */}
              {saveMessage && (
                <div
                  className={`text-sm text-center py-2 rounded-lg ${
                    saveMessage.includes("saved")
                      ? "bg-green-50 text-boz-success"
                      : "bg-red-50 text-boz-danger"
                  }`}
                >
                  {saveMessage}
                </div>
              )}

              <button
                onClick={handleSave}
                disabled={saving}
                className="min-h-[44px] w-full px-6 py-3 bg-boz-primary text-white rounded-lg font-medium hover:bg-blue-700 transition-colors disabled:opacity-50 flex items-center justify-center gap-2"
              >
                {saving ? (
                  <Loader2 size={16} className="animate-spin" />
                ) : (
                  <Save size={16} />
                )}
                {saving ? "Saving..." : "Save Settings"}
              </button>

              {/* About */}
              <section className="bg-white rounded-lg border border-gray-200 shadow-sm p-4">
                <h2 className="text-sm font-semibold mb-3">About</h2>
                <div className="flex items-center justify-between">
                  <span className="text-sm text-boz-neutral">Version</span>
                  <span className="text-sm font-medium" data-testid="current-version">
                    v{versionInfo?.current_version ?? "..."}
                  </span>
                </div>
                {versionInfo?.update_available && versionInfo.release_url && (
                  <div className="mt-3 bg-orange-50 border border-orange-200 rounded-lg p-3">
                    <div className="flex items-center justify-between">
                      <div>
                        <p className="text-sm font-medium text-orange-800">
                          Update available
                        </p>
                        <p className="text-xs text-orange-600">
                          v{versionInfo.latest_version} is now available
                        </p>
                      </div>
                      <a
                        href={versionInfo.release_url}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="px-3 py-1.5 bg-orange-600 text-white text-xs font-medium rounded-lg hover:bg-orange-700 transition-colors"
                        data-testid="update-link"
                      >
                        View Release
                      </a>
                    </div>
                    {/* Self-update button */}
                    {(!updateStatus || updateStatus.status === "idle" || updateStatus.status === "error") && (
                      <button
                        onClick={handleUpdate}
                        className="mt-3 w-full min-h-[44px] px-4 py-2 bg-boz-primary text-white text-sm font-medium rounded-lg hover:bg-blue-700 transition-colors flex items-center justify-center gap-2"
                        data-testid="update-button"
                      >
                        Update &amp; Restart
                      </button>
                    )}
                    {/* Update in progress */}
                    {updateStatus && !["idle", "done", "error"].includes(updateStatus.status) && (
                      <div className="mt-3 flex items-center gap-2" data-testid="update-progress">
                        <Loader2 size={14} className="animate-spin text-orange-600" />
                        <span className="text-xs text-orange-700 font-medium">
                          {updateStatus.status === "pulling" && "Pulling latest code..."}
                          {updateStatus.status === "building" && "Building Docker images..."}
                          {updateStatus.status === "restarting" && "Restarting containers..."}
                        </span>
                      </div>
                    )}
                    {/* Update done */}
                    {updateStatus?.status === "done" && (
                      <div className="mt-3 bg-green-50 border border-green-200 rounded-lg p-2" data-testid="update-done">
                        <p className="text-xs text-green-700 font-medium">
                          Update complete! Reload the page to see the new version.
                        </p>
                        <button
                          onClick={() => window.location.reload()}
                          className="mt-1 text-xs text-green-600 underline hover:text-green-800"
                        >
                          Reload now
                        </button>
                      </div>
                    )}
                    {/* Update error */}
                    {updateStatus?.status === "error" && (
                      <div className="mt-3 bg-red-50 border border-red-200 rounded-lg p-2" data-testid="update-error">
                        <p className="text-xs text-red-700 font-medium">
                          Update failed: {updateStatus.error || "Unknown error"}
                        </p>
                        <button
                          onClick={handleUpdate}
                          className="mt-1 text-xs text-red-600 underline hover:text-red-800"
                        >
                          Retry
                        </button>
                      </div>
                    )}
                  </div>
                )}
                {versionInfo && !versionInfo.update_available && versionInfo.latest_version && (
                  <p className="text-xs text-boz-success mt-2" data-testid="up-to-date">
                    You&apos;re running the latest version
                  </p>
                )}
              </section>

              {/* Disconnect */}
              <section className="border-t border-gray-200 pt-6">
                <button
                  onClick={handleDisconnect}
                  disabled={disconnecting}
                  className="min-h-[44px] w-full px-6 py-3 bg-white border border-boz-danger text-boz-danger rounded-lg font-medium hover:bg-red-50 transition-colors disabled:opacity-50 flex items-center justify-center gap-2"
                >
                  {disconnecting ? (
                    <Loader2 size={16} className="animate-spin" />
                  ) : null}
                  {disconnecting ? "Disconnecting..." : "Disconnect Kalshi Account"}
                </button>
                <p className="text-xs text-boz-neutral text-center mt-2">
                  This will delete all stored credentials and trade data.
                </p>
              </section>
            </div>
          )}
        </>
      ) : (
        <LogsViewer />
      )}
    </ErrorBoundary>
  );
}
