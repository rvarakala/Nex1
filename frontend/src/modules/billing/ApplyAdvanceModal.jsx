/**
 * ApplyAdvanceModal — Phase 2B.3 (UX)
 * -----------------------------------
 * Applies an active Advance Receipt to an existing invoice for the
 * SAME patient / tenant / branch context. Reuses the closed Phase 2B.2
 * allocation endpoint verbatim:
 *     POST /api/advance-receipts/{receipt_id}/allocations
 * Never invents a parallel financial mechanism.
 *
 * Governance rules enforced client-side (backend remains authoritative):
 *   • same tenant + same patient (tenant filter is server-side)
 *   • amount > 0, amount ≤ min(available, invoice.due_total)
 *   • mandatory Idempotency-Key (regenerated per attempt)
 *   • advance is a PAYMENT, NOT a discount — invoice.grand_total is
 *     displayed unchanged.
 */
import React, { useCallback, useEffect, useMemo, useState } from 'react';
import axios from 'axios';
import { X, Search, ExternalLink, FilePlus2 } from 'lucide-react';

const API = `${process.env.REACT_APP_BACKEND_URL}/api`;

const fmtINR = (n) => {
  if (n === null || n === undefined || Number.isNaN(Number(n))) return '—';
  return `₹${Number(n).toLocaleString('en-IN', { maximumFractionDigits: 2, minimumFractionDigits: 2 })}`;
};

const genIdemKey = () => {
  const rnd = (globalThis.crypto?.randomUUID?.() || Math.random().toString(36).slice(2));
  return `aa-ui-${Date.now().toString(36)}-${rnd.replace(/-/g, '').slice(0, 16)}`;
};

/**
 * receipt shape (subset):
 *   { receipt_id, receipt_no, patient_id, patient_name,
 *     received_amount, available_balance, allocated_total, status }
 */
export default function ApplyAdvanceModal({ receipt, onClose, onApplied }) {
  const receiptAvailable = useMemo(() => {
    // Phase 2B.2 exposes `available_balance` on new rows. Legacy rows
    // (missing field, null value) are rejected by the backend with a
    // clear 409 — we surface the same posture in the UI by treating
    // them as "not applicable yet".
    const v = receipt?.available_balance;
    if (v === null || v === undefined) return null;
    return Number(v);
  }, [receipt]);

  const isLegacyReceipt = receiptAvailable === null;

  const [invoices, setInvoices] = useState([]);
  const [loadingInvoices, setLoadingInvoices] = useState(true);
  const [selectedInvoiceId, setSelectedInvoiceId] = useState('');
  const [amount, setAmount] = useState('');
  const [amountTouched, setAmountTouched] = useState(false);
  const [search, setSearch] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState('');
  const [successBody, setSuccessBody] = useState(null);

  // Load candidate invoices for this patient (open due only).
  useEffect(() => {
    if (!receipt?.patient_id || isLegacyReceipt) { setLoadingInvoices(false); return; }
    let cancelled = false;
    (async () => {
      setLoadingInvoices(true);
      try {
        // Fetch by patient_id; then client-side filter to invoices with
        // a real outstanding balance and a status that Phase 2B.2 will
        // accept (not cancelled/refunded/partially_refunded).
        const r = await axios.get(`${API}/billing/invoices`, {
          params: { patient_id: receipt.patient_id, limit: 200 },
        });
        const items = Array.isArray(r.data) ? r.data : (r.data?.items || []);
        const eligible = items.filter((inv) => {
          const due = Number(inv?.due_total ?? 0);
          const s = String(inv?.status || '').toLowerCase();
          const blocked = ['cancelled', 'refunded', 'partially_refunded'].includes(s);
          return due > 0.01 && !blocked;
        });
        if (!cancelled) setInvoices(eligible);
      } catch (e) {
        if (!cancelled) setError(`Failed to load invoices: ${e?.response?.data?.detail || e.message}`);
      } finally {
        if (!cancelled) setLoadingInvoices(false);
      }
    })();
    return () => { cancelled = true; };
  }, [receipt?.patient_id, isLegacyReceipt]);

  const filteredInvoices = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return invoices;
    return invoices.filter((inv) => (
      (inv.invoice_no || '').toLowerCase().includes(q) ||
      (inv.status || '').toLowerCase().includes(q)
    ));
  }, [invoices, search]);

  const selectedInvoice = useMemo(
    () => invoices.find((inv) => inv.invoice_id === selectedInvoiceId) || null,
    [invoices, selectedInvoiceId],
  );

  // Auto-suggest amount = min(available, due) when user picks an invoice
  // (unless they have already typed one).
  useEffect(() => {
    if (!selectedInvoice || amountTouched) return;
    const due = Number(selectedInvoice.due_total || 0);
    const suggested = Math.min(receiptAvailable || 0, due);
    setAmount(String(suggested.toFixed(2)));
  }, [selectedInvoice, amountTouched, receiptAvailable]);

  const parsedAmount = Number(String(amount).replace(/,/g, '')) || 0;
  const amountError = useMemo(() => {
    if (isLegacyReceipt) return null; // banner already covers this
    if (!selectedInvoice) return null;
    if (!parsedAmount || parsedAmount <= 0) return 'Enter an amount greater than ₹0';
    if (parsedAmount > (receiptAvailable + 0.01)) {
      return `Cannot exceed available advance (${fmtINR(receiptAvailable)})`;
    }
    const due = Number(selectedInvoice.due_total || 0);
    if (parsedAmount > due + 0.01) {
      return `Cannot exceed invoice outstanding (${fmtINR(due)})`;
    }
    return null;
  }, [parsedAmount, selectedInvoice, receiptAvailable, isLegacyReceipt]);

  const canSubmit = !!selectedInvoice && !amountError && !submitting && !successBody && !isLegacyReceipt;

  const submit = useCallback(async () => {
    if (!canSubmit) return;
    setSubmitting(true);
    setError('');
    try {
      const r = await axios.post(
        `${API}/advance-receipts/${receipt.receipt_id}/allocations`,
        {
          invoice_id: selectedInvoice.invoice_id,
          amount: Number(parsedAmount.toFixed(2)),
        },
        { headers: { 'Idempotency-Key': genIdemKey() } },
      );
      setSuccessBody(r.data);
      if (typeof onApplied === 'function') onApplied(r.data);
    } catch (e) {
      const detail = e?.response?.data?.detail || e.message || 'Allocation failed';
      setError(detail);
    } finally {
      setSubmitting(false);
    }
  }, [canSubmit, receipt?.receipt_id, selectedInvoice, parsedAmount, onApplied]);

  return (
    <div
      className="fixed inset-0 z-50 bg-slate-900/60 backdrop-blur-sm flex items-center justify-center p-4"
      data-testid="apply-advance-modal"
      onClick={onClose}
    >
      <div
        className="bg-white rounded-2xl shadow-2xl w-full max-w-2xl max-h-[90vh] overflow-hidden border border-slate-200 flex flex-col"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="px-5 py-4 border-b border-slate-200 flex items-start justify-between">
          <div>
            <h3 className="text-[15px] font-bold text-slate-900" data-testid="apply-advance-title">
              Apply Advance {isLegacyReceipt ? '' : `— ${fmtINR(receiptAvailable)}`}
            </h3>
            <p className="text-[12px] text-slate-500 mt-0.5">
              Patient: <span className="font-semibold text-slate-700">{receipt?.patient_name || receipt?.patient_id}</span>
              {' · '}Advance Receipt: <span className="font-mono">{receipt?.receipt_no}</span>
            </p>
          </div>
          <button
            onClick={onClose}
            data-testid="apply-advance-close-btn"
            className="text-slate-500 hover:text-slate-900"
            title="Close"
          >
            <X size={18} />
          </button>
        </div>

        {/* Body */}
        <div className="flex-1 overflow-y-auto px-5 py-4 space-y-4">
          {/* Legacy receipt guard */}
          {isLegacyReceipt && (
            <div className="p-3 rounded-lg border border-amber-300 bg-amber-50 text-amber-900 text-[12px]" data-testid="apply-advance-legacy-banner">
              <b>This receipt was created before Phase 2B.2 and its balance ledger has not been initialised.</b>
              <div className="mt-1">
                A controlled backfill is required before it can be allocated. Contact your administrator.
              </div>
            </div>
          )}

          {/* Summary row */}
          {!isLegacyReceipt && (
            <div className="grid grid-cols-3 gap-2">
              <SummaryPill label="Received" value={fmtINR(receipt?.received_amount)} tone="slate" />
              <SummaryPill label="Available" value={fmtINR(receiptAvailable)} tone="emerald" testid="apply-advance-available" />
              <SummaryPill label="Already Applied" value={fmtINR(receipt?.allocated_total || 0)} tone="indigo" />
            </div>
          )}

          {/* Success */}
          {successBody && (
            <div className="p-4 rounded-lg border border-emerald-300 bg-emerald-50" data-testid="apply-advance-success">
              <div className="text-[13px] font-semibold text-emerald-900">
                Advance applied · {successBody.allocation_no}
              </div>
              <table className="w-full mt-2 text-[12px]">
                <tbody>
                  <tr><td className="text-slate-500">Invoice</td><td className="text-right font-mono">{successBody.invoice_no}</td></tr>
                  <tr><td className="text-slate-500">Amount applied</td><td className="text-right font-semibold">{fmtINR(successBody.amount)}</td></tr>
                  <tr><td className="text-slate-500">Invoice total (unchanged)</td><td className="text-right">{fmtINR(successBody?.invoice?.grand_total || successBody?.invoice?.rounded_total)}</td></tr>
                  <tr><td className="text-slate-500">Invoice paid total</td><td className="text-right text-emerald-700 font-semibold">{fmtINR(successBody?.invoice?.paid_total)}</td></tr>
                  <tr><td className="text-slate-500">Invoice balance due</td><td className="text-right text-rose-700 font-semibold">{fmtINR(successBody?.invoice?.due_total)}</td></tr>
                  <tr><td className="text-slate-500">Remaining advance</td><td className="text-right text-emerald-700 font-semibold">{fmtINR(successBody?.advance_receipt?.available_balance)}</td></tr>
                </tbody>
              </table>
            </div>
          )}

          {/* Target selection — Apply to Existing Invoice */}
          {!isLegacyReceipt && !successBody && (
            <>
              <div>
                <div className="flex items-center justify-between mb-2">
                  <label className="text-[11px] uppercase tracking-wider text-slate-500 font-semibold">
                    Apply to · Existing Invoice
                  </label>
                  <span className="text-[11px] text-slate-400">
                    {loadingInvoices ? 'Loading…' : `${filteredInvoices.length} eligible`}
                  </span>
                </div>
                <div className="relative mb-2">
                  <Search size={13} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-slate-400" />
                  <input
                    value={search}
                    onChange={(e) => setSearch(e.target.value)}
                    placeholder="Search invoice #…"
                    className="w-full pl-8 pr-3 py-1.5 border border-slate-300 rounded-lg text-[12px] bg-white"
                    data-testid="apply-advance-invoice-search"
                  />
                </div>

                <div className="max-h-52 overflow-y-auto border border-slate-200 rounded-lg divide-y divide-slate-100">
                  {loadingInvoices ? (
                    <div className="text-center py-6 text-slate-400 italic text-[12px]">Loading invoices…</div>
                  ) : filteredInvoices.length === 0 ? (
                    <div className="text-center py-6 text-slate-400 italic text-[12px]" data-testid="apply-advance-no-invoices">
                      No open invoices for this patient. Create the sale first — Hearing Aid, Custom HA, Ear Mould, or Accessory — and then reopen this dialog to apply the advance.
                    </div>
                  ) : filteredInvoices.map((inv, i) => (
                    <button
                      key={inv.invoice_id}
                      type="button"
                      onClick={() => { setSelectedInvoiceId(inv.invoice_id); setAmountTouched(false); }}
                      data-testid={`apply-advance-invoice-option-${i}`}
                      className={`w-full text-left px-3 py-2 text-[12px] transition ${
                        selectedInvoiceId === inv.invoice_id
                          ? 'bg-sky-50 border-l-4 border-sky-500'
                          : 'hover:bg-slate-50 border-l-4 border-transparent'
                      }`}
                    >
                      <div className="flex justify-between items-baseline">
                        <span className="font-mono font-semibold text-slate-800">{inv.invoice_no}</span>
                        <span className="text-[10px] uppercase tracking-wide text-slate-500">{inv.status}</span>
                      </div>
                      <div className="flex justify-between text-[11px] text-slate-500 mt-0.5">
                        <span>Total {fmtINR(inv.grand_total || inv.rounded_total)}</span>
                        <span className="text-rose-700 font-semibold">Due {fmtINR(inv.due_total)}</span>
                      </div>
                    </button>
                  ))}
                </div>
              </div>

              {/* Amount input */}
              {selectedInvoice && (
                <div>
                  <label className="block text-[11px] uppercase tracking-wider text-slate-500 font-semibold mb-1">
                    Amount to apply
                  </label>
                  <div className="flex items-center gap-2">
                    <div className="flex-1 relative">
                      <span className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-400 font-semibold">₹</span>
                      <input
                        type="number"
                        min="0"
                        step="0.01"
                        value={amount}
                        onChange={(e) => { setAmount(e.target.value); setAmountTouched(true); }}
                        data-testid="apply-advance-amount-input"
                        className={`w-full pl-7 pr-3 py-2 border rounded-lg text-sm font-semibold ${
                          amountError ? 'border-rose-400 bg-rose-50' : 'border-slate-300 bg-white'
                        }`}
                      />
                    </div>
                    <button
                      type="button"
                      onClick={() => {
                        const due = Number(selectedInvoice.due_total || 0);
                        setAmount(String(Math.min(receiptAvailable, due).toFixed(2)));
                        setAmountTouched(true);
                      }}
                      className="text-[11px] px-2 py-1 rounded border border-slate-300 text-slate-600 hover:bg-slate-100"
                      data-testid="apply-advance-amount-max-btn"
                    >
                      Max
                    </button>
                  </div>
                  {amountError && (
                    <div className="text-[11px] text-rose-700 mt-1" data-testid="apply-advance-amount-error">{amountError}</div>
                  )}

                  {/* Live preview — invoice total is preserved */}
                  <div className="mt-3 p-3 rounded-lg bg-slate-50 border border-slate-200 text-[12px]">
                    <div className="font-semibold text-slate-700 mb-1">Preview after allocation</div>
                    <table className="w-full">
                      <tbody>
                        <tr>
                          <td className="text-slate-500">Invoice total <span className="text-[10px] text-slate-400">(unchanged)</span></td>
                          <td className="text-right">{fmtINR(selectedInvoice.grand_total || selectedInvoice.rounded_total)}</td>
                        </tr>
                        <tr>
                          <td className="text-slate-500">Advance applied</td>
                          <td className="text-right text-emerald-700 font-semibold">− {fmtINR(parsedAmount)}</td>
                        </tr>
                        <tr>
                          <td className="text-slate-500">Balance still due</td>
                          <td className="text-right text-rose-700 font-bold">
                            {fmtINR(Math.max(0, Number(selectedInvoice.due_total || 0) - parsedAmount))}
                          </td>
                        </tr>
                        <tr>
                          <td className="text-slate-500">Advance remaining</td>
                          <td className="text-right text-slate-800 font-semibold">
                            {fmtINR(Math.max(0, receiptAvailable - parsedAmount))}
                          </td>
                        </tr>
                      </tbody>
                    </table>
                  </div>
                </div>
              )}
            </>
          )}

          {/* Error banner */}
          {error && (
            <div className="p-3 rounded-lg border border-rose-300 bg-rose-50 text-rose-800 text-[12px]" data-testid="apply-advance-error">
              {String(error)}
            </div>
          )}

          {/* Helper — deep-links for NEW sales */}
          {!isLegacyReceipt && !successBody && !loadingInvoices && invoices.length === 0 && (
            <div className="grid grid-cols-2 gap-2 pt-2 border-t border-slate-100" data-testid="apply-advance-newsale-hints">
              <NewSaleHint to="/ha/fittings" label="New Hearing Aid Sale" />
              <NewSaleHint to="/ha/custom-orders" label="New Custom HA Order" />
              <NewSaleHint to="/ha/ear-moulds" label="New Ear Mould" />
              <NewSaleHint to="/ha/accessories" label="New Accessory Sale" />
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="px-5 py-3 border-t border-slate-200 flex items-center justify-end gap-2 bg-slate-50">
          <button
            type="button"
            onClick={onClose}
            data-testid="apply-advance-cancel-btn"
            className="px-3 py-1.5 text-[13px] font-semibold text-slate-600 hover:text-slate-900 rounded-lg hover:bg-slate-100"
          >
            {successBody ? 'Done' : 'Cancel'}
          </button>
          {!successBody && (
            <button
              type="button"
              disabled={!canSubmit}
              onClick={submit}
              data-testid="apply-advance-confirm-btn"
              className="px-4 py-1.5 text-[13px] font-semibold rounded-lg text-white bg-sky-600 hover:bg-sky-700 disabled:opacity-50 disabled:cursor-not-allowed shadow-sm"
            >
              {submitting ? 'Applying…' : `Apply ${parsedAmount > 0 ? fmtINR(parsedAmount) : ''}`.trim()}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

const SummaryPill = ({ label, value, tone = 'slate', testid }) => {
  const toneMap = {
    slate: 'bg-slate-50 text-slate-800 border-slate-200',
    emerald: 'bg-emerald-50 text-emerald-800 border-emerald-200',
    indigo: 'bg-indigo-50 text-indigo-800 border-indigo-200',
  };
  return (
    <div className={`px-3 py-2 rounded-lg border ${toneMap[tone]}`} data-testid={testid}>
      <div className="text-[9px] uppercase tracking-wider font-semibold opacity-70">{label}</div>
      <div className="text-[13px] font-bold mt-0.5">{value}</div>
    </div>
  );
};

const NewSaleHint = ({ to, label }) => (
  <a
    href={to}
    target="_blank"
    rel="noopener noreferrer"
    className="inline-flex items-center gap-1 px-2.5 py-1.5 rounded border border-slate-200 hover:border-sky-300 hover:bg-sky-50 text-[11px] text-slate-700"
  >
    <FilePlus2 size={12} className="text-sky-600" />
    <span className="truncate">{label}</span>
    <ExternalLink size={10} className="text-slate-400 ml-auto" />
  </a>
);
