/**
 * AdvanceReceiptModal
 * -------------------
 * Standalone modal to create an Advance Receipt / Payment Acknowledgement.
 * Used from Front Desk, Billing, and (indirectly) the Patient profile.
 *
 * Scope (Phase 2A · Receipt-only):
 *   • Accepts a positive `received_amount`, `method`, optional reference
 *     and purpose note.
 *   • Requires a resolved patient (id + name).
 *   • Sends a mandatory Idempotency-Key header (regenerated on every
 *     mount so consecutive submissions cannot silently deduplicate).
 *   • Does NOT create an invoice, does NOT touch inventory / serials,
 *     and does NOT include any GST fields.
 *
 * On success it invokes `onSuccess(receipt)` and opens the printable
 * acknowledgement in a new tab.
 */
import React, { useCallback, useMemo, useRef, useState } from 'react';
import axios from 'axios';
import { X, Receipt, ExternalLink, AlertTriangle } from 'lucide-react';

const API = process.env.REACT_APP_BACKEND_URL + '/api';

const METHODS = [
  { value: 'cash', label: 'Cash' },
  { value: 'upi', label: 'UPI' },
  { value: 'card', label: 'Card' },
  { value: 'bank_transfer', label: 'Bank Transfer' },
  { value: 'cheque', label: 'Cheque' },
  { value: 'insurance', label: 'Insurance' },
  { value: 'other', label: 'Other' },
];

const fmtINR = (n) => {
  if (n === null || n === undefined || Number.isNaN(Number(n))) return '—';
  return `₹${Number(n).toLocaleString('en-IN', { maximumFractionDigits: 2, minimumFractionDigits: 2 })}`;
};

const genIdempotencyKey = () => {
  const ts = Date.now().toString(36);
  const rand = Math.random().toString(36).slice(2, 12);
  return `ar-ui-${ts}-${rand}`;
};

export default function AdvanceReceiptModal({
  open,
  onClose,
  patient,          // { patient_id, name, mobile, mrd } — required
  onSuccess,        // (receipt) => void
}) {
  const idempotencyKeyRef = useRef(genIdempotencyKey());
  const [amount, setAmount] = useState('');
  const [method, setMethod] = useState('cash');
  const [reference, setReference] = useState('');
  const [purposeNote, setPurposeNote] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState(null);
  const [created, setCreated] = useState(null);   // successful receipt row

  const canSubmit = useMemo(() => {
    const amt = Number(amount);
    return !!(patient?.patient_id) && Number.isFinite(amt) && amt > 0 && method;
  }, [amount, method, patient]);

  const reset = useCallback(() => {
    idempotencyKeyRef.current = genIdempotencyKey();
    setAmount(''); setMethod('cash'); setReference('');
    setPurposeNote(''); setError(null); setCreated(null);
    setSubmitting(false);
  }, []);

  const handleClose = useCallback(() => {
    reset();
    onClose?.();
  }, [onClose, reset]);

  const openPrintable = useCallback((receipt) => {
    if (!receipt?.receipt_id) return;
    const url = `${API}/advance-receipts/${receipt.receipt_id}/receipt.pdf`;
    // Include auth via bearer token propagation is handled by axios interceptor;
    // for direct browser open we rely on cookie auth (set at login).
    window.open(url, '_blank', 'noopener,noreferrer');
  }, []);

  const submit = useCallback(async () => {
    if (!canSubmit || submitting) return;
    setSubmitting(true); setError(null);
    try {
      const body = {
        patient_id: patient.patient_id,
        received_amount: Number(amount),
        method,
        reference: reference.trim() || null,
        purpose_note: purposeNote.trim() || null,
      };
      const r = await axios.post(`${API}/advance-receipts`, body, {
        headers: { 'Idempotency-Key': idempotencyKeyRef.current },
      });
      setCreated(r.data);
      onSuccess?.(r.data);
    } catch (e) {
      const detail = e?.response?.data?.detail;
      const msg = Array.isArray(detail)
        ? detail.map((x) => x.msg || String(x)).join(', ')
        : (typeof detail === 'string' ? detail : (e?.message || 'Could not create receipt'));
      setError(msg);
    } finally {
      setSubmitting(false);
    }
  }, [amount, method, reference, purposeNote, patient, canSubmit, submitting, onSuccess]);

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-50 bg-slate-900/60 backdrop-blur-sm flex items-center justify-center p-4"
      data-testid="advance-receipt-modal"
      onClick={handleClose}
    >
      <div
        className="bg-white rounded-2xl shadow-2xl w-full max-w-lg overflow-hidden border border-slate-200"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="px-6 py-4 border-b border-slate-200 flex items-center justify-between bg-gradient-to-r from-sky-50 to-cyan-50">
          <div className="flex items-center gap-3">
            <div className="w-9 h-9 rounded-full bg-sky-600 text-white flex items-center justify-center shadow-sm">
              <Receipt size={18} />
            </div>
            <div>
              <h3 className="text-[15px] font-bold text-slate-900 tracking-tight">Advance Receipt</h3>
              <p className="text-[11px] text-slate-500">Payment Acknowledgement · Not a Tax Invoice</p>
            </div>
          </div>
          <button
            className="text-slate-500 hover:text-slate-800 p-1 rounded-full hover:bg-white transition"
            data-testid="advance-receipt-close-btn"
            onClick={handleClose}
          >
            <X size={18} />
          </button>
        </div>

        {/* Body */}
        {!created ? (
          <div className="px-6 py-4 space-y-4">
            <div className="bg-slate-50 border border-slate-200 rounded-lg px-3 py-2 text-[12px] text-slate-700" data-testid="advance-receipt-patient-summary">
              <div className="text-[10px] uppercase tracking-wider text-slate-500 mb-0.5">Patient</div>
              <div className="font-semibold text-slate-900">
                {patient?.name || '—'}
                {patient?.mrd && <span className="ml-2 text-slate-500 font-normal">· MRD {patient.mrd}</span>}
                {patient?.mobile && <span className="ml-2 text-slate-500 font-normal">· {patient.mobile}</span>}
              </div>
            </div>

            {error && (
              <div className="flex items-start gap-2 bg-rose-50 border border-rose-200 rounded-lg px-3 py-2 text-[12px] text-rose-800" data-testid="advance-receipt-error">
                <AlertTriangle size={14} className="mt-0.5 shrink-0" />
                <span>{error}</span>
              </div>
            )}

            <div>
              <label className="block text-[11px] font-semibold uppercase tracking-wider text-slate-500 mb-1">
                Amount Received (₹) *
              </label>
              <input
                type="number"
                min="0.01"
                step="0.01"
                value={amount}
                onChange={(e) => setAmount(e.target.value)}
                placeholder="0.00"
                data-testid="advance-receipt-amount-input"
                className="w-full px-3 py-2 border border-slate-300 rounded-lg text-sm focus:ring-2 focus:ring-sky-500 focus:border-sky-500 outline-none"
                autoFocus
              />
            </div>

            <div className="grid grid-cols-2 gap-3">
              <div>
                <label className="block text-[11px] font-semibold uppercase tracking-wider text-slate-500 mb-1">
                  Method *
                </label>
                <select
                  value={method}
                  onChange={(e) => setMethod(e.target.value)}
                  data-testid="advance-receipt-method-select"
                  className="w-full px-3 py-2 border border-slate-300 rounded-lg text-sm focus:ring-2 focus:ring-sky-500 focus:border-sky-500 outline-none bg-white"
                >
                  {METHODS.map((m) => (
                    <option key={m.value} value={m.value}>{m.label}</option>
                  ))}
                </select>
              </div>
              <div>
                <label className="block text-[11px] font-semibold uppercase tracking-wider text-slate-500 mb-1">
                  Reference # (optional)
                </label>
                <input
                  type="text"
                  value={reference}
                  onChange={(e) => setReference(e.target.value)}
                  placeholder="UPI txn, cheque #"
                  data-testid="advance-receipt-reference-input"
                  className="w-full px-3 py-2 border border-slate-300 rounded-lg text-sm focus:ring-2 focus:ring-sky-500 focus:border-sky-500 outline-none"
                />
              </div>
            </div>

            <div>
              <label className="block text-[11px] font-semibold uppercase tracking-wider text-slate-500 mb-1">
                Purpose Note (optional)
              </label>
              <textarea
                value={purposeNote}
                onChange={(e) => setPurposeNote(e.target.value)}
                rows={2}
                maxLength={500}
                placeholder="e.g., Advance for hearing-aid trial"
                data-testid="advance-receipt-purpose-input"
                className="w-full px-3 py-2 border border-slate-300 rounded-lg text-sm focus:ring-2 focus:ring-sky-500 focus:border-sky-500 outline-none resize-none"
              />
              <div className="text-[10px] text-slate-400 mt-1 italic">
                This note is informational only. The money is not bound to any product.
              </div>
            </div>

            <div className="bg-amber-50 border-l-4 border-amber-400 px-3 py-2 text-[11px] text-amber-900 leading-relaxed">
              <strong>Important:</strong> This receipt acknowledges cash-in-hand only.
              It is NOT a Tax Invoice, does NOT recognise revenue, does NOT include GST,
              and does NOT reserve inventory. A Tax Invoice will be issued separately
              when the product / service is finalised.
            </div>
          </div>
        ) : (
          <div className="px-6 py-6 text-center space-y-4" data-testid="advance-receipt-success">
            <div className="w-14 h-14 rounded-full bg-emerald-100 text-emerald-700 mx-auto flex items-center justify-center">
              <Receipt size={26} />
            </div>
            <div>
              <h4 className="text-base font-bold text-slate-900">Receipt Created</h4>
              <p className="text-[13px] text-slate-500 mt-1">
                <span className="font-mono">{created.receipt_no}</span>
                {' · '}
                <span className="font-semibold text-slate-900">{fmtINR(created.received_amount)}</span>
              </p>
            </div>
            <button
              onClick={() => openPrintable(created)}
              data-testid="advance-receipt-print-btn"
              className="inline-flex items-center gap-2 px-4 py-2 bg-sky-600 text-white text-sm font-semibold rounded-lg hover:bg-sky-700 transition shadow-sm"
            >
              <ExternalLink size={14} /> Open Printable Receipt
            </button>
          </div>
        )}

        {/* Footer */}
        <div className="px-6 py-3 border-t border-slate-200 flex items-center justify-end gap-2 bg-slate-50">
          {!created ? (
            <>
              <button
                onClick={handleClose}
                data-testid="advance-receipt-cancel-btn"
                className="px-4 py-2 text-slate-600 hover:text-slate-900 text-sm font-semibold"
              >
                Cancel
              </button>
              <button
                onClick={submit}
                disabled={!canSubmit || submitting}
                data-testid="advance-receipt-submit-btn"
                className="px-4 py-2 bg-sky-600 text-white text-sm font-semibold rounded-lg shadow-sm disabled:opacity-50 hover:bg-sky-700 transition"
              >
                {submitting ? 'Saving…' : `Receive ${amount ? fmtINR(amount) : 'Advance'}`}
              </button>
            </>
          ) : (
            <button
              onClick={handleClose}
              data-testid="advance-receipt-done-btn"
              className="px-4 py-2 bg-slate-700 text-white text-sm font-semibold rounded-lg hover:bg-slate-900 transition"
            >
              Done
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
