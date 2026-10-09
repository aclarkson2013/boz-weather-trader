/**
 * API client — typed fetch wrapper for all 13 backend endpoints.
 *
 * All functions throw on non-OK responses. 401 errors redirect to /onboarding.
 */

import type {
  AuthStatusResponse,
  AuthValidateRequest,
  AuthValidateResponse,
  BracketPrediction,
  CalibrationReport,
  CalendarMonth,
  CityCode,
  CurrentWeatherResponse,
  DashboardData,
  DashboardStats,
  LogEntry,
  PendingTrade,
  PerformanceData,
  PushSubscriptionPayload,
  SettingsUpdate,
  SourceAccuracy,
  SyncResult,
  TradeRecord,
  TradesPage,
  TrainingReportList,
  UpdateStatus,
  UpdateTriggerResponse,
  UserSettings,
  VersionInfo,
} from "./types";

const FALLBACK_API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

/**
 * Derive the API base URL at runtime from the browser's current hostname.
 * This allows the same build to work over LAN (10.0.0.51) and Tailscale (100.x.y.z).
 * Falls back to NEXT_PUBLIC_API_URL during SSR or when window is unavailable.
 */
function getApiUrl(): string {
  if (typeof window !== "undefined" && window.location?.hostname) {
    return `${window.location.protocol}//${window.location.hostname}:8000`;
  }
  return FALLBACK_API_URL;
}

// ─── WebSocket URL ───

/**
 * Derive the WebSocket URL from the current hostname.
 * Converts http: → ws: and https: → wss:, then appends /ws.
 */
export function getWsUrl(): string {
  const base = getApiUrl();
  return base.replace(/^http/, "ws") + "/ws";
}

// ─── Fetch Wrapper ───

class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

async function apiFetch<T>(
  path: string,
  options: RequestInit = {}
): Promise<T> {
  const url = `${getApiUrl()}${path}`;

  const res = await fetch(url, {
    headers: {
      "Content-Type": "application/json",
      ...options.headers,
    },
    ...options,
  });

  // 401 → redirect to onboarding (unless already there)
  if (res.status === 401) {
    if (
      typeof window !== "undefined" &&
      !window.location.pathname.startsWith("/onboarding")
    ) {
      window.location.href = "/onboarding";
    }
    let detail = "Not authenticated";
    try {
      const body = await res.json();
      detail = body.detail || body.message || detail;
    } catch {
      // Use default message
    }
    throw new ApiError(detail, 401);
  }

  // 204 No Content → return undefined
  if (res.status === 204) {
    return undefined as T;
  }

  if (!res.ok) {
    let message = `Request failed: ${res.status}`;
    try {
      const body = await res.json();
      message = body.detail || body.message || message;
    } catch {
      // Use default message
    }
    throw new ApiError(message, res.status);
  }

  return res.json();
}

// ─── Auth (2 endpoints) ───

export async function validateCredentials(
  creds: AuthValidateRequest
): Promise<AuthValidateResponse> {
  return apiFetch<AuthValidateResponse>("/api/auth/validate", {
    method: "POST",
    body: JSON.stringify(creds),
  });
}

export async function fetchAuthStatus(): Promise<AuthStatusResponse> {
  return apiFetch<AuthStatusResponse>("/api/auth/status");
}

export async function disconnect(): Promise<void> {
  return apiFetch<void>("/api/auth/disconnect", {
    method: "POST",
  });
}

// ─── Dashboard (2 endpoints) ───

export async function fetchDashboard(): Promise<DashboardData> {
  return apiFetch<DashboardData>("/api/dashboard");
}

export async function fetchDashboardStats(): Promise<DashboardStats> {
  return apiFetch<DashboardStats>("/api/dashboard/stats");
}

export async function fetchCooldownStatus(): Promise<import("./types").CooldownStatus> {
  return apiFetch<import("./types").CooldownStatus>("/api/dashboard/stats/cooldown");
}

// ─── Markets (1 endpoint) ───

export async function fetchMarkets(
  city?: CityCode
): Promise<BracketPrediction[]> {
  const params = city ? `?city=${city}` : "";
  return apiFetch<BracketPrediction[]>(`/api/markets${params}`);
}

// ─── Trades (2 endpoints) ───

export async function fetchTrades(
  page: number = 1,
  city?: CityCode,
  status?: string,
  date?: string,
  perPage?: number,
): Promise<TradesPage> {
  const params = new URLSearchParams({ page: String(page) });
  if (city) params.set("city", city);
  if (status) params.set("status", status);
  if (date) params.set("trade_date", date);
  if (perPage) params.set("per_page", String(perPage));
  return apiFetch<TradesPage>(`/api/trades?${params.toString()}`);
}

export async function syncTrades(): Promise<SyncResult> {
  return apiFetch<SyncResult>("/api/trades/sync", {
    method: "POST",
  });
}

// ─── Queue (3 endpoints) ───

export async function fetchPendingTrades(): Promise<PendingTrade[]> {
  return apiFetch<PendingTrade[]>("/api/queue");
}

export async function approveTrade(
  tradeId: string
): Promise<TradeRecord> {
  return apiFetch<TradeRecord>(`/api/queue/${tradeId}/approve`, {
    method: "POST",
  });
}

export async function rejectTrade(tradeId: string): Promise<void> {
  return apiFetch<void>(`/api/queue/${tradeId}/reject`, {
    method: "POST",
  });
}

// ─── Settings (2 endpoints) ───

export async function fetchSettings(): Promise<UserSettings> {
  return apiFetch<UserSettings>("/api/settings");
}

export async function updateSettings(
  update: SettingsUpdate
): Promise<UserSettings> {
  return apiFetch<UserSettings>("/api/settings", {
    method: "PATCH",
    body: JSON.stringify(update),
  });
}

// ─── Logs (1 endpoint) ───

export async function fetchLogs(params?: {
  module?: string;
  level?: string;
  after?: string;
}): Promise<LogEntry[]> {
  const searchParams = new URLSearchParams();
  if (params?.module) searchParams.set("module", params.module);
  if (params?.level) searchParams.set("level", params.level);
  if (params?.after) searchParams.set("after", params.after);
  const qs = searchParams.toString();
  return apiFetch<LogEntry[]>(`/api/logs${qs ? `?${qs}` : ""}`);
}

// ─── Performance (1 endpoint) ───

export async function fetchPerformance(): Promise<PerformanceData> {
  return apiFetch<PerformanceData>("/api/performance");
}

// ─── Notifications (1 endpoint) ───

export async function subscribePush(
  subscription: PushSubscriptionPayload
): Promise<void> {
  return apiFetch<void>("/api/notifications/subscribe", {
    method: "POST",
    body: JSON.stringify(subscription),
  });
}

// ─── Accuracy (2 endpoints) ───

export async function fetchCalibration(
  city: CityCode = "NYC",
  lookbackDays: number = 90
): Promise<CalibrationReport> {
  return apiFetch<CalibrationReport>(
    `/api/accuracy/calibration?city=${city}&lookback_days=${lookbackDays}`
  );
}

export async function fetchSourceAccuracy(
  city: CityCode = "NYC",
  lookbackDays: number = 90
): Promise<SourceAccuracy[]> {
  return apiFetch<SourceAccuracy[]>(
    `/api/accuracy/sources?city=${city}&lookback_days=${lookbackDays}`
  );
}

export async function fetchModelEdge(
  lookbackDays: number = 90
): Promise<import("./types").ModelEdgeReport> {
  return apiFetch<import("./types").ModelEdgeReport>(
    `/api/accuracy/edge?lookback_days=${lookbackDays}`
  );
}

// ─── Paper Test (algo v2) ───

export async function fetchPaperStrategies(): Promise<
  import("./types").PaperStrategySummary[]
> {
  return apiFetch<import("./types").PaperStrategySummary[]>("/api/paper/strategies");
}

// ─── Weather (1 endpoint) ───

export async function fetchCurrentWeather(): Promise<CurrentWeatherResponse> {
  return apiFetch<CurrentWeatherResponse>("/api/weather/current");
}

// ─── Version (3 endpoints) ───

export async function fetchVersion(): Promise<VersionInfo> {
  return apiFetch<VersionInfo>("/api/version");
}

export async function triggerUpdate(): Promise<UpdateTriggerResponse> {
  return apiFetch<UpdateTriggerResponse>("/api/version/update", {
    method: "POST",
  });
}

export async function fetchUpdateStatus(): Promise<UpdateStatus> {
  return apiFetch<UpdateStatus>("/api/version/update/status");
}

// ─── Training Reports (2 endpoints) ───

export async function fetchTrainingReports(
  limit: number = 10,
  offset: number = 0
): Promise<TrainingReportList> {
  return apiFetch<TrainingReportList>(
    `/api/training/reports?limit=${limit}&offset=${offset}`
  );
}

export async function triggerRetraining(): Promise<UpdateTriggerResponse> {
  return apiFetch<UpdateTriggerResponse>("/api/training/trigger", {
    method: "POST",
  });
}

// ─── Calendar (1 endpoint) ───

export async function fetchCalendar(
  year: number,
  month: number
): Promise<CalendarMonth> {
  return apiFetch<CalendarMonth>(
    `/api/trades/calendar?year=${year}&month=${month}`
  );
}
