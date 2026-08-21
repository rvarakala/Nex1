/**
 * AdvanceReceiptsPage
 * -------------------
 * Clinic-scoped ledger of Advance Receipts (Phase 2A · Receipt-only).
 * Mounted under Billing → "Advances" tab. Front-desk and accounts staff
 * use this to open a new Advance Receipt from a picked patient, review
 * history, void mistakes, and reprint acknowledgements.
 *
 * Explicit scope boundary:
 *   • No allocation-to-invoice UI (Phase 2B).
 *   • No refund-of-advance UI (Phase 2C).
 *   • No inventory linkage.
 */
import React, { useCallback, useEffect, useMemo, useState } from 'react';
import axios from 'axios';
import { Link } from 'react-router-dom';
import { Search, Plus, ExternalLink, Ban, RotateCcw } from 'lucide-react';
import AdvanceReceiptModal from './AdvanceReceiptModal';
import { useAuth } from '../../AuthContext';

const API = process.env.REACT_APP_BACKEND_URL + '/api';

const fmtINR = (n) => {
  if (n === null || n === undefined || Number.isNaN(Number(n))) return '—';
  return `₹${Number(n).toLocaleString('en-IN', { maximumFractionDigits: 2, minimumFractionDigits: 2 })}`;
};

const fmtDateTime = (iso) => {
  if (!iso) return '—';
  try { return new Date(iso).toLocaleString('en-IN', { day: '2-digit', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit' }); }
  catch { return iso; }
};

const STATUS_BADGE = {
  active: 'bg-emerald-100 text-emerald-800 border-emerald-300',
  voided: 'bg-rose-100 text-rose-700 border-rose-300 line-through',
};

export default function AdvanceReceiptsPage() {
  const { user } = useAuth();
  const canCreate = ['front_desk', 'accounts', 'clinic_owner', 'super_admin', 'founder'].includes(user?.role);
  const canVoid = ['accounts', 'clinic_owner', 'super_admin', 'founder'].includes(user?.role);

  const [rows, setRows] = useState([]);
  const [activeTotal, setActiveTotal] = useState(0);
  const [statusFilter, setStatusFilter] = useState('');
  const [search, setSearch] = useState('');
  const [loading, setLoading] = useState(true);
  const [showModal, setShowModal] = useState(false);
  const [pickedPatient, setPickedPatient] = useState(null);
  const [patientQuery, setPatientQuery] = useState('');
  const [patientResults, setPatientResults] = useState([]);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const url = new URL(`${API}/advance-receipts`);
      if (statusFilter) url.searchParams.set('status', statusFilter);
      const r = await axios.get(url.toString());
      const items = Array.isArray(r.data?.items) ? r.data.items : (Array.isArray(r.data) ? r.data : []);
      setRows(items);
      setActiveTotal(Number(r.data?.active_total || 0));
    } catch {
      setRows([]); setActiveTotal(0);
    } finally { setLoading(false); }
  }, [statusFilter]);

  useEffect(() => { load(); }, [load]);

  // Patient picker (debounced) — reuses same search endpoint as billing/create-invoice.
  useEffect(() => {
    if (!patientQuery || patientQuery.length < 2) { setPatientResults([]); return; }
    const t = setTimeout(async () => {
      try {
        const r = await axios.get(`${API}/patients/search`, { params: { q: patientQuery, limit: 8 } });
        setPatientResults(Array.isArray(r.data) ? r.data : (r.data?.items || []));
      } catch { setPatientResults([]); }
    }, 250);
    return () => clearTimeout(t);
  }, [patientQuery]);

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return rows;
    return rows.filter((r) => (
      (r.receipt_no || '').toLowerCase().includes(q) ||
      (r.patient_name || '').toLowerCase().includes(q) ||
      (r.patient_mobile || '').toLowerCase().includes(q) ||
      (r.patient_mrd || '').toLowerCase().includes(q) ||
      (r.reference || '').toLowerCase().includes(q)
    ));
  }, [rows, search]);

  const openReceipt = useCallback((receiptId) => {
    window.open(`${API}/advance-receipts/${receiptId}/receipt.pdf`, '_blank', 'noopener,noreferrer');
  }, []);

  const voidReceipt = useCallback(async (receiptId, receiptNo) => {
    const reason = prompt(`Void ${receiptNo}?\nPlease enter a reason (mandatory):`);
    if (!reason || reason.trim().length < 3) return;
    try {
      await axios.post(`${API}/advance-receipts/${receiptId}/void`, { reason: reason.trim() });
      load();
    } catch (e) {
      alert('Could not void: ' + (e?.response?.data?.detail || e.message));
    }
  }, [load]);

  return (
    <div className="p-4 sm:p-6 space-y-4" data-testid="advance-receipts-page">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3">
        <div>
          <h2 className="text-lg font-bold text-slate-900 tracking-tight">Advance Receipts</h2>
          <p className="text-[12px] text-slate-500">
            Payment acknowledgements collected before final invoicing. Not tax invoices.
          </p>
        </div>
        {canCreate && (
          <div className="relative">
            <button
              onClick={() => { setPickedPatient(null); setPatientQuery(''); setShowModal(true); }}
              data-testid="advance-receipts-new-btn"
              className="inline-flex items-center gap-2 px-4 py-2 bg-sky-600 text-white text-[13px] font-semibold rounded-lg shadow-sm hover:bg-sky-700 transition"
            >
              <Plus size={14} /> New Advance Receipt
            </button>
          </div>
        )}
      </div>

      {/* Summary tile */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        <SummaryTile label="Active Total" value={fmtINR(activeTotal)} tone="emerald" testid="advance-summary-active-total" />
        <SummaryTile label="Total Rows" value={String(rows.length)} tone="slate" testid="advance-summary-total-rows" />
        <SummaryTile label="Active" value={String(rows.filter((r) => r.status === 'active').length)} tone="emerald" testid="advance-summary-active-count" />
        <SummaryTile label="Voided" value={String(rows.filter((r) => r.status === 'voided').length)} tone="rose" testid="advance-summary-voided-count" />
      </div>

      {/* Filters */}
      <div className="flex flex-wrap items-center gap-2">
        <div className="relative flex-1 min-w-[200px]">
          <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-400" />
          <input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search by receipt #, patient, MRD, ref…"
            data-testid="advance-receipts-search-input"
            className="w-full pl-9 pr-3 py-2 border border-slate-300 rounded-lg text-sm bg-white"
          />
        </div>
        <select
          value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value)}
          data-testid="advance-receipts-status-filter"
          className="px-3 py-2 border border-slate-300 rounded-lg text-sm bg-white"
        >
          <option value="">All statuses</option>
          <option value="active">Active</option>
          <option value="voided">Voided</option>
        </select>
        <button
          onClick={load}
          data-testid="advance-receipts-refresh-btn"
          className="p-2 border border-slate-300 rounded-lg text-slate-600 hover:bg-slate-100 transition"
          title="Refresh"
        >
          <RotateCcw size={14} />
        </button>
      </div>

      {/* Table */}
      <div className="bg-white rounded-xl border border-slate-200 overflow-hidden shadow-sm">
        <table className="w-full text-[13px]" data-testid="advance-receipts-table">
          <thead className="bg-slate-50 text-[10.5px] uppercase tracking-wider text-slate-500 border-b border-slate-200">
            <tr>
              <th className="text-left px-3 py-2.5 font-semibold">Receipt #</th>
              <th className="text-left px-3 py-2.5 font-semibold">Patient</th>
              <th className="text-right px-3 py-2.5 font-semibold">Amount</th>
              <th className="text-left px-3 py-2.5 font-semibold">Method</th>
              <th className="text-left px-3 py-2.5 font-semibold">Received</th>
              <th className="text-left px-3 py-2.5 font-semibold">Status</th>
              <th className="text-right px-3 py-2.5 font-semibold">Actions</th>
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <tr><td colSpan={7} className="text-center py-8 text-slate-400 italic">Loading…</td></tr>
            ) : filtered.length === 0 ? (
              <tr><td colSpan={7} className="text-center py-10 text-slate-400 italic">No advance receipts yet.</td></tr>
            ) : filtered.map((r, i) => (
              <tr key={r.receipt_id} className="border-b border-slate-100 hover:bg-slate-50" data-testid={`advance-receipt-row-${i}`}>
                <td className="px-3 py-2.5 font-mono text-[12px] text-slate-800">{r.receipt_no}</td>
                <td className="px-3 py-2.5">
                  <Link
                    to={`/patients/${r.patient_id}?tab=advances`}
                    className="text-indigo-700 font-semibold hover:underline"
                  >
                    {r.patient_name || r.patient_id}
                  </Link>
                  {r.patient_mobile && <div className="text-[11px] text-slate-500">{r.patient_mobile}</div>}
                </td>
                <td className="px-3 py-2.5 text-right font-bold text-slate-900">{fmtINR(r.received_amount)}</td>
                <td className="px-3 py-2.5 capitalize text-slate-700">{String(r.method || '').replace('_', ' ')}</td>
                <td className="px-3 py-2.5 text-slate-600 text-[12px]">{fmtDateTime(r.received_at)}</td>
                <td className="px-3 py-2.5">
                  <span className={`text-[10px] px-1.5 py-0.5 font-bold rounded border uppercase tracking-wider ${STATUS_BADGE[r.status] || STATUS_BADGE.active}`} data-testid={`advance-receipt-status-${i}`}>
                    {r.status}
                  </span>
                </td>
                <td className="px-3 py-2.5 text-right space-x-1">
                  <button
                    onClick={() => openReceipt(r.receipt_id)}
                    data-testid={`advance-receipt-print-btn-${i}`}
                    className="inline-flex items-center gap-1 px-2 py-1 text-[11px] font-semibold text-sky-700 hover:text-sky-900 hover:bg-sky-50 rounded"
                    title="Open printable receipt"
                  >
                    <ExternalLink size={12} /> Print
                  </button>
                  {canVoid && r.status === 'active' && (
                    <button
                      onClick={() => voidReceipt(r.receipt_id, r.receipt_no)}
                      data-testid={`advance-receipt-void-btn-${i}`}
                      className="inline-flex items-center gap-1 px-2 py-1 text-[11px] font-semibold text-rose-700 hover:text-rose-900 hover:bg-rose-50 rounded"
                      title="Void receipt"
                    >
                      <Ban size={12} /> Void
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* Modal — creation flow */}
      {showModal && !pickedPatient && (
        <div className="fixed inset-0 z-40 bg-slate-900/60 backdrop-blur-sm flex items-center justify-center p-4" data-testid="advance-receipts-patient-picker">
          <div className="bg-white rounded-2xl shadow-2xl w-full max-w-md p-5 space-y-4 border border-slate-200" onClick={(e) => e.stopPropagation()}>
            <div className="flex items-center justify-between">
              <h3 className="text-[15px] font-bold text-slate-900">Select Patient</h3>
              <button
                onClick={() => setShowModal(false)}
                data-testid="advance-receipts-picker-close-btn"
                className="text-slate-500 hover:text-slate-900 text-sm"
              >
                ✕
              </button>
            </div>
            <input
              value={patientQuery}
              onChange={(e) => setPatientQuery(e.target.value)}
              placeholder="Type name / mobile / MRD (min 2 chars)"
              autoFocus
              data-testid="advance-receipts-patient-search-input"
              className="w-full px-3 py-2 border border-slate-300 rounded-lg text-sm focus:ring-2 focus:ring-sky-500 outline-none"
            />
            <div className="max-h-60 overflow-y-auto space-y-1">
              {patientResults.map((p, i) => (
                <button
                  key={p.patient_id}
                  onClick={() => setPickedPatient(p)}
                  data-testid={`advance-receipts-patient-result-${i}`}
                  className="w-full text-left px-3 py-2 rounded-lg border border-slate-200 hover:bg-sky-50 hover:border-sky-300 transition"
                >
                  <div className="font-semibold text-slate-900 text-[13px]">{p.name}</div>
                  <div className="text-[11px] text-slate-500">
                    {p.mrd && <>MRD {p.mrd} · </>}{p.mobile || '—'}
                  </div>
                </button>
              ))}
              {patientQuery.length >= 2 && patientResults.length === 0 && (
                <div className="text-[12px] text-slate-400 italic text-center py-4">No matches.</div>
              )}
            </div>
          </div>
        </div>
      )}

      {pickedPatient && (
        <AdvanceReceiptModal
          open={showModal}
          patient={pickedPatient}
          onClose={() => { setShowModal(false); setPickedPatient(null); }}
          onSuccess={() => { load(); }}
        />
      )}
    </div>
  );
}

const SummaryTile = ({ label, value, tone = 'slate', testid }) => {
  const toneMap = {
    emerald: 'bg-emerald-50 text-emerald-800 border-emerald-200',
    rose: 'bg-rose-50 text-rose-800 border-rose-200',
    slate: 'bg-slate-50 text-slate-800 border-slate-200',
  };
  return (
    <div className={`px-4 py-3 rounded-xl border ${toneMap[tone]}`} data-testid={testid}>
      <div className="text-[10.5px] uppercase tracking-wider font-semibold opacity-70">{label}</div>
      <div className="text-lg font-bold mt-1">{value}</div>
    </div>
  );
};
