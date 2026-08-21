/**
 * AllocationsListModal — Phase 2B.3 (UX)
 * --------------------------------------
 * Read-only audit-trail viewer for an Advance Receipt's allocation
 * ledger. Never mutates data. Uses the tenant-scoped GET endpoint
 * added on top of the Phase 2B.2 collection:
 *     GET /api/advance-receipts/{receipt_id}/allocations
 */
import React, { useEffect, useState } from 'react';
import axios from 'axios';
import { X, ExternalLink } from 'lucide-react';

const API = `${process.env.REACT_APP_BACKEND_URL}/api`;

const fmtINR = (n) => {
  if (n === null || n === undefined || Number.isNaN(Number(n))) return '—';
  return `₹${Number(n).toLocaleString('en-IN', { maximumFractionDigits: 2, minimumFractionDigits: 2 })}`;
};

const fmtDT = (iso) => {
  if (!iso) return '—';
  try {
    return new Date(iso).toLocaleString('en-IN', {
      day: '2-digit', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit',
    });
  } catch { return iso; }
};

const STATUS_BADGE = {
  active: 'bg-emerald-100 text-emerald-800 border-emerald-300',
  voided: 'bg-rose-100 text-rose-700 border-rose-300 line-through',
};

export default function AllocationsListModal({ receipt, onClose }) {
  const [state, setState] = useState({ loading: true, items: [], summary: null, error: '' });

  useEffect(() => {
    if (!receipt?.receipt_id) return;
    let cancelled = false;
    (async () => {
      try {
        const r = await axios.get(`${API}/advance-receipts/${receipt.receipt_id}/allocations`);
        if (cancelled) return;
        setState({
          loading: false,
          items: r.data?.items || [],
          summary: r.data?.receipt || null,
          totals: {
            active: r.data?.total_active_amount || 0,
            voided: r.data?.total_voided_amount || 0,
          },
          error: '',
        });
      } catch (e) {
        if (!cancelled) setState({
          loading: false, items: [], summary: null, totals: null,
          error: e?.response?.data?.detail || e.message || 'Failed to load allocations',
        });
      }
    })();
    return () => { cancelled = true; };
  }, [receipt?.receipt_id]);

  return (
    <div
      className="fixed inset-0 z-50 bg-slate-900/60 backdrop-blur-sm flex items-center justify-center p-4"
      data-testid="allocations-list-modal"
      onClick={onClose}
    >
      <div
        className="bg-white rounded-2xl shadow-2xl w-full max-w-2xl max-h-[90vh] overflow-hidden border border-slate-200 flex flex-col"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="px-5 py-4 border-b border-slate-200 flex items-start justify-between">
          <div>
            <h3 className="text-[15px] font-bold text-slate-900">Allocations · {receipt?.receipt_no}</h3>
            <p className="text-[12px] text-slate-500 mt-0.5">
              Patient: <span className="font-semibold text-slate-700">{receipt?.patient_name || receipt?.patient_id}</span>
            </p>
          </div>
          <button onClick={onClose} data-testid="allocations-list-close-btn" className="text-slate-500 hover:text-slate-900">
            <X size={18} />
          </button>
        </div>

        <div className="flex-1 overflow-y-auto px-5 py-4 space-y-3">
          {state.summary && (
            <div className="grid grid-cols-3 gap-2 text-[12px]">
              <div className="px-3 py-2 rounded-lg border border-slate-200 bg-slate-50">
                <div className="text-[9px] uppercase tracking-wider text-slate-500 font-semibold">Received</div>
                <div className="font-bold text-slate-900 mt-0.5">{fmtINR(state.summary.received_amount)}</div>
              </div>
              <div className="px-3 py-2 rounded-lg border border-emerald-200 bg-emerald-50">
                <div className="text-[9px] uppercase tracking-wider text-emerald-700 font-semibold">Available</div>
                <div className="font-bold text-emerald-900 mt-0.5">{fmtINR(state.summary.available_balance)}</div>
              </div>
              <div className="px-3 py-2 rounded-lg border border-indigo-200 bg-indigo-50">
                <div className="text-[9px] uppercase tracking-wider text-indigo-700 font-semibold">Applied · Active</div>
                <div className="font-bold text-indigo-900 mt-0.5">{fmtINR(state.summary.allocated_total)}</div>
              </div>
            </div>
          )}

          {state.error && (
            <div className="p-3 rounded-lg border border-rose-300 bg-rose-50 text-rose-800 text-[12px]">
              {state.error}
            </div>
          )}

          <div className="bg-white rounded-xl border border-slate-200 overflow-hidden">
            <table className="w-full text-[12px]" data-testid="allocations-list-table">
              <thead className="bg-slate-50 text-[10px] uppercase tracking-wider text-slate-500 border-b border-slate-200">
                <tr>
                  <th className="text-left px-3 py-2 font-semibold">Allocation #</th>
                  <th className="text-left px-3 py-2 font-semibold">Invoice #</th>
                  <th className="text-right px-3 py-2 font-semibold">Amount</th>
                  <th className="text-left px-3 py-2 font-semibold">When</th>
                  <th className="text-left px-3 py-2 font-semibold">Status</th>
                </tr>
              </thead>
              <tbody>
                {state.loading ? (
                  <tr><td colSpan={5} className="text-center py-6 text-slate-400 italic">Loading…</td></tr>
                ) : state.items.length === 0 ? (
                  <tr><td colSpan={5} className="text-center py-8 text-slate-400 italic" data-testid="allocations-list-empty">No allocations yet.</td></tr>
                ) : state.items.map((a, i) => (
                  <tr key={a.allocation_id} className="border-b border-slate-100" data-testid={`allocation-row-${i}`}>
                    <td className="px-3 py-2 font-mono text-slate-800">{a.allocation_no}</td>
                    <td className="px-3 py-2 font-mono text-slate-800">
                      {a.invoice_no || a.invoice_id}
                    </td>
                    <td className="px-3 py-2 text-right font-bold text-slate-900">{fmtINR(a.amount)}</td>
                    <td className="px-3 py-2 text-slate-600">{fmtDT(a.created_at)}</td>
                    <td className="px-3 py-2">
                      <span className={`text-[9.5px] px-1.5 py-0.5 font-bold rounded border uppercase tracking-wider ${STATUS_BADGE[a.status] || STATUS_BADGE.active}`}>
                        {a.status}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="text-[11px] text-slate-400 italic border-t border-slate-100 pt-2">
            Advance is applied as a <b>payment</b> against the invoice · the invoice total is never reduced.
            Voiding an allocation is a Phase 2B.3+ feature (not implemented in this build).
          </div>
        </div>

        <div className="px-5 py-3 border-t border-slate-200 flex items-center justify-end bg-slate-50">
          <button
            onClick={onClose}
            data-testid="allocations-list-done-btn"
            className="px-3 py-1.5 text-[13px] font-semibold text-slate-600 hover:text-slate-900 rounded-lg hover:bg-slate-100"
          >
            Close
          </button>
        </div>
      </div>
    </div>
  );
}
