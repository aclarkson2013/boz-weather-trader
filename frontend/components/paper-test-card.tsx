"use client";

import { FlaskConical } from "lucide-react";
import type { PaperStrategySummary } from "@/lib/types";
import { formatPnL } from "@/lib/utils";

/** Pre-registered verdict needs >= 300 traded city-days and >= ~6 months. */
export const VERDICT_MIN_TRADED_DAYS = 300;
export const VERDICT_MIN_DAYS = 182;

const DESCRIPTIONS: Record<string, string> = {
  L5: "Bets against long-shot temperatures (YES priced 1–4¢) at 8 PM the evening before.",
  B5: "Station forecast model combined with market prices, at 5 PM the day before (B3, unchanged).",
};

/** Days elapsed since an ISO timestamp (0 if missing). */
export function daysSince(iso: string | null, now: Date = new Date()): number {
  if (!iso) return 0;
  const start = new Date(iso.endsWith("Z") ? iso : `${iso}Z`);
  return Math.max(
    0,
    Math.floor((now.getTime() - start.getTime()) / 86_400_000),
  );
}

/**
 * How close a strategy is to its automatic shut-off, 0 (safe) .. 1 (stopped).
 * Uses whichever check is closer: SPRT (model strategies) or CUSUM (losses).
 */
export function shutOffProximity(s: PaperStrategySummary): number {
  const cusum = s.cusum_cents != null ? s.cusum_cents / s.cusum_alarm_cents : 0;
  let sprt = 0;
  if (s.sprt_llr != null && s.sprt_llr < 0) {
    sprt = s.sprt_llr / s.sprt_stop; // both negative -> 0..1
  }
  return Math.min(1, Math.max(0, cusum, sprt));
}

function statusBadge(s: PaperStrategySummary) {
  if (s.status === "stopped") {
    return (
      <span className="text-[11px] px-2 py-0.5 rounded-full bg-red-100 text-boz-danger">
        Stopped
      </span>
    );
  }
  if (s.status === "active") {
    return (
      <span className="text-[11px] px-2 py-0.5 rounded-full bg-green-100 text-boz-success">
        Running
      </span>
    );
  }
  return (
    <span className="text-[11px] px-2 py-0.5 rounded-full bg-gray-100 text-boz-neutral">
      Not started
    </span>
  );
}

interface Props {
  strategies: PaperStrategySummary[];
  now?: Date;
}

/** Forward paper test progress — pretend trades on live prices, no real money. */
export default function PaperTestCard({ strategies, now }: Props) {
  return (
    <div className="bg-white rounded-lg border border-gray-200 shadow-sm p-4">
      <div className="flex items-center gap-2 mb-1">
        <FlaskConical className="w-4 h-4 text-boz-primary" />
        <h3 className="text-sm font-semibold">Paper Test</h3>
      </div>
      <p className="text-xs text-boz-neutral mb-4">
        Pretend trades on live Kalshi prices — no real money. A verdict needs
        about 6 months; a strategy is shut off automatically if it is clearly
        losing.
      </p>

      <div className="space-y-4">
        {strategies.map((s) => {
          const days = daysSince(s.started_at, now);
          const settled = s.trades_settled;
          const winRate =
            settled > 0 ? Math.round((s.wins / settled) * 100) : null;
          const proximity = shutOffProximity(s);
          const progress = Math.min(
            1,
            Math.min(
              s.traded_city_days / VERDICT_MIN_TRADED_DAYS,
              days / VERDICT_MIN_DAYS,
            ),
          );
          return (
            <div
              key={s.strategy_id}
              data-testid={`paper-${s.strategy_id}`}
              className="border border-gray-100 rounded-md p-3"
            >
              <div className="flex items-center justify-between mb-1">
                <span className="text-sm font-semibold">{s.strategy_id}</span>
                {statusBadge(s)}
              </div>
              <p className="text-xs text-boz-neutral mb-2">
                {DESCRIPTIONS[s.strategy_id] ?? ""}
              </p>

              <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 text-xs mb-2">
                <div>
                  <div className="text-boz-neutral">Pretend P&L</div>
                  <div
                    className={`font-bold ${s.pnl_cents >= 0 ? "text-boz-success" : "text-boz-danger"}`}
                  >
                    {formatPnL(s.pnl_cents)}
                  </div>
                </div>
                <div>
                  <div className="text-boz-neutral">Trades</div>
                  <div className="font-medium">
                    {settled} settled · {s.trades_open} open
                  </div>
                </div>
                <div>
                  <div className="text-boz-neutral">Win rate</div>
                  <div className="font-medium">
                    {winRate == null ? "—" : `${winRate}%`}
                  </div>
                </div>
                <div>
                  <div className="text-boz-neutral">Days running</div>
                  <div className="font-medium">
                    {s.status === "not_started" ? "—" : days}
                  </div>
                </div>
              </div>

              <div className="text-[11px] text-boz-neutral mb-1">
                Progress to verdict: {s.traded_city_days} /{" "}
                {VERDICT_MIN_TRADED_DAYS} trading days
              </div>
              <div className="bg-gray-100 rounded-full h-2 overflow-hidden mb-2">
                <div
                  className="h-full bg-boz-primary"
                  style={{ width: `${progress * 100}%` }}
                />
              </div>

              <div className="text-[11px] text-boz-neutral mb-1">
                Shut-off check:{" "}
                {s.status === "stopped"
                  ? "stopped"
                  : proximity >= 0.66
                    ? "getting close"
                    : "safe"}
              </div>
              <div className="bg-gray-100 rounded-full h-2 overflow-hidden">
                <div
                  className={`h-full ${proximity >= 0.66 ? "bg-boz-danger" : "bg-boz-success"}`}
                  style={{
                    width: `${(s.status === "stopped" ? 1 : proximity) * 100}%`,
                  }}
                />
              </div>

              {s.stop_reason && (
                <p className="text-xs text-boz-danger mt-2">
                  Stopped: {s.stop_reason}
                </p>
              )}
              {s.infeasible_fills > 0 && (
                <p className="text-[11px] text-boz-neutral mt-2">
                  {s.infeasible_fills} pretend fill(s) were larger than the
                  order book showed.
                </p>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
