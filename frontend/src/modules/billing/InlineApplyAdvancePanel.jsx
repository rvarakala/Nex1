/**
 * InlineApplyAdvancePanel — Phase 2B.3 (UX Correction)
 * ----------------------------------------------------
 * Embedded, controlled panel that lets clinic staff pick an existing
 * Advance Receipt to apply as a payment line WHILE creating a sale
 * (HA / Custom HA / Ear Mould / Accessory invoice). The parent form
 * calls its `preflight()` before submitting, then calls `postAllocate()`
 * with the fresh `invoice_id` after the invoice is created.
 *
 * DELIBERATELY NEVER writes financial state on its own:
 *   • It only READS `GET /api/advance-receipts` for discovery.
 *   • The authoritative writer remains `POST /api/advance-receipts/{id}/allocations`
 *     — the closed Phase 2B.2 endpoint. All atomicity, CAS, idempotency,
 *     tenant / patient / RBAC / over-allocation / concurrency guards
 *     live there. The panel neither reimplements nor bypasses them.
 *
 * Failure semantics (deliberately explicit — no silent phantom payments):
 *   • preflight() re-fetches the selected receipt right before the sale
 *     POST. If the current available_balance is less than the requested
 *     amount (concurrent race), the parent BLOCKS the sale and surfaces
 *     the fresh balance. No invoice is created.
 *   • postAllocate() runs AFTER the invoice exists. If the allocation
 *     POST fails at that point, the parent MUST show the error and leave
 *     the invoice untouched (grand_total preserved, no phantom advance
 *     payment recorded). It is safe because Phase 2B.2 either succeeds
 *     atomically or does nothing.
 *
 * Accounting invariant:
 *   The advance is a PAYMENT, NOT a discount. The sale POST still
 *   uses the full sale price. The advance shows as a *separate* payment
 *   line on the invoice (method="advance") after the allocation lands.
 */
import React, { useCallback, useEffect, useMemo, useState } from 'react';
import axios from 'axios';
import { IndianRupee, Info } from 'lucide-react';

const API = `${process.env.REACT_APP_BACKEND_URL}/api`;

const MONEY_TOL = 0.01;

const fmtINR = (n) => {
  if (n === null || n === undefined || Number.isNaN(Number(n))) return '—';
  return `₹${Number(n).toLocaleString('en-IN', { maximumFractionDigits: 2, minimumFractionDigits: 2 })}`;
};

export const genAllocIdemKey = () => {
  const rnd = (globalThis.crypto?.randomUUID?.() || Math.random().toString(36).slice(2));
  return `aa-inline-${Date.now().toString(36)}-${rnd.replace(/-/g, '').slice(0, 16)}`;
};

/**
 * Controlled component.
 *
 * Props:
 *   patientId    string   — required; render nothing when falsy
 *   salePrice    number   — the invoice's grand total; caps the amount
 *   value        object   — { enabled, receiptId, amount } (controlled state)
 *   onChange     fn(value) — parent-owned state setter
 *   disabled     boolean  — hide/lock during submit
 *   testidPrefix string   — for scoped data-testids (default "inline-apply-advance")
 */
export default function InlineApplyAdvancePanel({
  patientId,
  salePrice,
  value,
  onChange,
  disabled = false,
  testidPrefix = 'inline-apply-advance',
}) {
  const [receipts, setReceipts] = useState([]);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState('');

  // Fetch this patient's active, non-legacy receipts with real balance.
  useEffect(() => {
    if (!patientId) { setReceipts([]); return; }
    let cancelled = false;
    (async () => {
      setLoading(true);
      setLoadError('');
      try {
        const r = await axios.get(`${API}/advance-receipts`, {
          params: { patient_id: patientId, status: 'active', limit: 100 },
        });
        const items = (r.data?.items || []).filter((it) => {
          const v = it?.available_balance;
          return v !== null && v !== undefined && Number(v) > MONEY_TOL;
        });
        if (!cancelled) setReceipts(items);
      } catch (e) {
        if (!cancelled) setLoadError(e?.response?.data?.detail || e.message || 'Failed to load advances');
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [patientId]);

  const totalAvailable = useMemo(
    () => receipts.reduce((s, r) => s + Number(r.available_balance || 0), 0),
    [receipts],
  );

  const selectedReceipt = useMemo(
    () => receipts.find((r) => r.receipt_id === value?.receiptId) || null,
    [receipts, value?.receiptId],
  );

  const cap = useMemo(() => {
    if (!selectedReceipt) return 0;
    const avail = Number(selectedReceipt.available_balance || 0);
    const sale = Number(salePrice || 0);
    return Math.max(0, Math.min(avail, sale));
  }, [selectedReceipt, salePrice]);

  const parsedAmount = Number(String(value?.amount ?? '').replace(/,/g, '')) || 0;

  const amountError = useMemo(() => {
    if (!value?.enabled) return null;
    if (!selectedReceipt) return 'Pick an advance receipt';
    if (parsedAmount <= 0) return 'Enter an amount greater than ₹0';
    if (parsedAmount > Number(selectedReceipt.available_balance || 0) + MONEY_TOL) {
      return `Cannot exceed available advance (${fmtINR(selectedReceipt.available_balance)})`;
    }
    if (Number(salePrice || 0) > 0 && parsedAmount > Number(salePrice) + MONEY_TOL) {
      return `Cannot exceed sale value (${fmtINR(salePrice)})`;
    }
    return null;
  }, [value?.enabled, selectedReceipt, parsedAmount, salePrice]);

  // Auto-select the highest-balance receipt when toggled ON and suggest amount.
  const onToggle = (enabled) => {
    if (!enabled) {
      onChange?.({ enabled: false, receiptId: null, amount: '' });
      return;
    }
    const best = receipts.slice().sort(
      (a, b) => Number(b.available_balance || 0) - Number(a.available_balance || 0),
    )[0];
    if (!best) { onChange?.({ enabled: true, receiptId: null, amount: '' }); return; }
    const suggested = Math.min(
      Number(best.available_balance || 0),
      Number(salePrice || 0) || Number(best.available_balance || 0),
    );
    onChange?.({ enabled: true, receiptId: best.receipt_id, amount: String(suggested.toFixed(2)) });
  };

  const onPickReceipt = (receiptId) => {
    const r = receipts.find((x) => x.receipt_id === receiptId);
    if (!r) { onChange?.({ ...(value || {}), receiptId: null }); return; }
    const suggested = Math.min(
      Number(r.available_balance || 0),
      Number(salePrice || 0) || Number(r.available_balance || 0),
    );
    onChange?.({ enabled: true, receiptId, amount: String(suggested.toFixed(2)) });
  };

  const onAmountChange = (raw) => {
    onChange?.({ ...(value || {}), amount: raw });
  };

  const onMax = () => {
    if (!selectedReceipt) return;
    onChange?.({ ...(value || {}), amount: String(cap.toFixed(2)) });
  };

  // Nothing to render if this patient has no usable advance.
  if (!patientId) return null;
  if (loading) {
    return (
      <div className="text-[11px] text-slate-500 italic px-3 py-2" data-testid={`${testidPrefix}-loading`}>
        Checking advance receipts…
      </div>
    );
  }
  if (loadError) {
    return (
      <div className="text-[11px] text-rose-700 bg-rose-50 border border-rose-200 rounded px-3 py-2" data-testid={`${testidPrefix}-load-error`}>
        {loadError}
      </div>
    );
  }
  if (receipts.length === 0) return null;

  const balancePreview = Math.max(0, Number(salePrice || 0) - (value?.enabled ? parsedAmount : 0));

  return (
    <div
      className={`rounded-lg border ${value?.enabled ? 'border-emerald-300 bg-emerald-50/50' : 'border-slate-200 bg-white'} p-3 space-y-2 ${disabled ? 'opacity-70 pointer-events-none' : ''}`}
      data-testid={testidPrefix}
    >
      <label className="flex items-start gap-2 cursor-pointer">
        <input
          type="checkbox"
          checked={!!value?.enabled}
          onChange={(e) => onToggle(e.target.checked)}
          data-testid={`${testidPrefix}-toggle`}
          className="mt-0.5 rounded text-emerald-600"
        />
        <div className="flex-1">
          <div className="flex items-center gap-1.5 text-[13px] font-semibold text-slate-800">
            <IndianRupee size={13} className="text-emerald-700" />
            Apply Advance — {fmtINR(totalAvailable)} available
            <span className="text-[10px] font-normal text-slate-500 ml-1">
              ({receipts.length} active receipt{receipts.length > 1 ? 's' : ''})
            </span>
          </div>
          <div className="text-[11px] text-slate-500 mt-0.5 flex items-center gap-1">
            <Info size={11} />
            Advance will be recorded as a <b>payment</b> on the invoice, not a discount. Invoice total stays at {fmtINR(salePrice)}.
          </div>
        </div>
      </label>

      {value?.enabled && (
        <div className="pl-6 space-y-2">
          {/* Receipt picker — dropdown when multiple, badge when single */}
          {receipts.length === 1 ? (
            <div className="text-[11px] text-slate-700">
              <span className="text-slate-500">Receipt:</span>{' '}
              <span className="font-mono font-semibold" data-testid={`${testidPrefix}-single-receipt`}>
                {receipts[0].receipt_no}
              </span>
              {' · '}
              <span className="text-emerald-700 font-semibold">
                {fmtINR(receipts[0].available_balance)} available
              </span>
            </div>
          ) : (
            <div>
              <label className="block text-[10px] uppercase tracking-wider text-slate-500 font-semibold mb-1">
                Choose advance receipt
              </label>
              <select
                value={value?.receiptId || ''}
                onChange={(e) => onPickReceipt(e.target.value)}
                data-testid={`${testidPrefix}-receipt-select`}
                className="w-full border border-slate-300 rounded px-2 py-1.5 text-[12px] bg-white"
              >
                <option value="">— pick a receipt —</option>
                {receipts.map((r) => (
                  <option key={r.receipt_id} value={r.receipt_id}>
                    {r.receipt_no} · {fmtINR(r.available_balance)} available
                  </option>
                ))}
              </select>
            </div>
          )}

          {/* Amount input */}
          {selectedReceipt && (
            <div>
              <label className="block text-[10px] uppercase tracking-wider text-slate-500 font-semibold mb-1">
                Amount to apply
              </label>
              <div className="flex items-center gap-2">
                <div className="flex-1 relative">
                  <span className="absolute left-2.5 top-1/2 -translate-y-1/2 text-slate-400 font-semibold text-sm">₹</span>
                  <input
                    type="number"
                    min="0"
                    step="0.01"
                    value={value?.amount ?? ''}
                    onChange={(e) => onAmountChange(e.target.value)}
                    data-testid={`${testidPrefix}-amount`}
                    className={`w-full pl-6 pr-2 py-1.5 border rounded text-[13px] font-semibold tabular-nums ${
                      amountError ? 'border-rose-400 bg-rose-50' : 'border-slate-300 bg-white'
                    }`}
                  />
                </div>
                <button
                  type="button"
                  onClick={onMax}
                  data-testid={`${testidPrefix}-max`}
                  className="text-[11px] px-2 py-1 rounded border border-slate-300 text-slate-600 hover:bg-slate-100 font-semibold"
                >
                  Max
                </button>
              </div>
              {amountError && (
                <div className="text-[11px] text-rose-700 mt-1" data-testid={`${testidPrefix}-amount-error`}>
                  {amountError}
                </div>
              )}

              {/* Live preview */}
              <div className="mt-2 grid grid-cols-3 gap-2 text-[11px]" data-testid={`${testidPrefix}-preview`}>
                <PreviewPill label="Invoice total (unchanged)" value={fmtINR(salePrice)} tone="slate" />
                <PreviewPill label="Advance applied" value={`− ${fmtINR(parsedAmount)}`} tone="emerald" />
                <PreviewPill label="Balance to collect" value={fmtINR(balancePreview)} tone="rose" />
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

const PreviewPill = ({ label, value, tone = 'slate' }) => {
  const toneMap = {
    slate: 'bg-white border-slate-200 text-slate-800',
    emerald: 'bg-emerald-50 border-emerald-200 text-emerald-800',
    rose: 'bg-rose-50 border-rose-200 text-rose-800',
  };
  return (
    <div className={`px-2 py-1.5 rounded border ${toneMap[tone]}`}>
      <div className="text-[9px] uppercase tracking-wider font-semibold opacity-70">{label}</div>
      <div className="text-[12px] font-bold mt-0.5 tabular-nums">{value}</div>
    </div>
  );
};

/**
 * preflightAdvance — call BEFORE creating the sale invoice.
 * Re-fetches the selected receipt and confirms available_balance ≥ amount.
 * Throws Error with a user-friendly message on failure. Caller catches
 * the error and blocks the submit.
 *
 * Returns the fresh receipt document on success.
 */
export async function preflightAdvance({ receiptId, amount }) {
  const r = await axios.get(`${API}/advance-receipts/${encodeURIComponent(receiptId)}`);
  const fresh = r.data;
  if (!fresh) throw new Error('Advance receipt not found');
  if (fresh.status !== 'active') {
    throw new Error(`Advance receipt is ${fresh.status}. Refresh and choose another.`);
  }
  const avail = Number(fresh.available_balance || 0);
  if (avail + MONEY_TOL < Number(amount || 0)) {
    throw new Error(
      `Advance no longer has ${fmtINR(amount)} available — only ${fmtINR(avail)} remaining. Refresh and re-enter.`,
    );
  }
  return fresh;
}

/**
 * allocateAdvance — call AFTER the sale invoice is created.
 * POSTs to the authoritative Phase 2B.2 writer. Fresh Idempotency-Key
 * per attempt. Throws Error with a user-friendly message on failure.
 * The invoice is NOT rolled back on failure (there is no phantom
 * advance payment — Phase 2B.2 is atomic).
 *
 * Returns the allocation response body on success.
 */
export async function allocateAdvance({ receiptId, invoiceId, amount }) {
  const r = await axios.post(
    `${API}/advance-receipts/${encodeURIComponent(receiptId)}/allocations`,
    { invoice_id: invoiceId, amount: Number(Number(amount).toFixed(2)) },
    { headers: { 'Idempotency-Key': genAllocIdemKey() } },
  );
  return r.data;
}
