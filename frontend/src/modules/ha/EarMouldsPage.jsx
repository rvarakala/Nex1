/*
 * Ear Moulds — Feb 2026 (quick-book variant)
 *
 * Simple book-and-forget workflow for custom ear moulds. Captures
 * patient + specs + advance payment in one modal, generates a full
 * PARTIAL / PAID / UNPAID invoice in the shared billing collection,
 * and stamps a soft workflow status on the order for chase-and-collect.
 *
 * Backend: /api/ha/ear-moulds (POST, GET, PATCH /{id}/status)
 */
import React, { useEffect, useMemo, useState, useCallback } from 'react';
import axios from 'axios';
import { Link } from 'react-router-dom';
import { Plus, Ear, Search, Calendar, Package, RefreshCw } from 'lucide-react';
import PatientAdvancesBanner from '../billing/PatientAdvancesBanner';

const API = `${process.env.REACT_APP_BACKEND_URL}/api`;

const STATUS_ORDER = ['pending_impression', 'sent_to_lab', 'arrived', 'delivered', 'cancelled'];
const STATUS_META = {
  pending_impression: { label: 'Impression Pending', tone: 'bg-slate-100 text-slate-700 border-slate-200' },
  sent_to_lab:        { label: 'Sent to Lab',        tone: 'bg-amber-100 text-amber-800 border-amber-300' },
  arrived:            { label: 'Arrived',            tone: 'bg-indigo-100 text-indigo-800 border-indigo-300' },
  delivered:          { label: 'Delivered',          tone: 'bg-emerald-100 text-emerald-800 border-emerald-300' },
  cancelled:          { label: 'Cancelled',          tone: 'bg-rose-100 text-rose-800 border-rose-300' },
};
const SIDE_LABEL = { left: 'Left', right: 'Right', both: 'Both' };

function fmtDay(d) {
  if (!d) return '—';
  return new Date(d).toLocaleDateString('en-IN', { day: '2-digit', month: 'short', year: 'numeric' });
}
function fmtMoney(n) {
  return `₹${Number(n || 0).toLocaleString('en-IN')}`;
}

export default function EarMouldsPage() {
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(true);
  const [showBook, setShowBook] = useState(false);
  const [statusFilter, setStatusFilter] = useState('');
  const [search, setSearch] = useState('');

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const r = await axios.get(`${API}/ha/ear-moulds`, {
        params: statusFilter ? { status: statusFilter } : {},
      });
      setRows(r.data || []);
    } finally {
      setLoading(false);
    }
  }, [statusFilter]);
  useEffect(() => { load(); }, [load]);

  const filtered = useMemo(() => {
    if (!search.trim()) return rows;
    const q = search.toLowerCase();
    return rows.filter((r) =>
      (r.patient_name || '').toLowerCase().includes(q) ||
      (r.order_no || '').toLowerCase().includes(q) ||
      (r.lab_vendor || '').toLowerCase().includes(q)
    );
  }, [rows, search]);

  const kpis = useMemo(() => {
    const c = { total: rows.length, sent: 0, arrived: 0, dueBalance: 0 };
    rows.forEach((r) => {
      if (r.status === 'sent_to_lab') c.sent++;
      if (r.status === 'arrived') c.arrived++;
      c.dueBalance += Number(r.balance_due || 0);
    });
    return c;
  }, [rows]);

  return (
    <div className="p-4 sm:p-6" data-testid="ha-ear-moulds-page">
      <div className="flex flex-wrap items-center justify-between gap-3 mb-4">
        <div>
          <h1 className="text-2xl font-bold tracking-tight text-slate-800 flex items-center gap-2">
            <Ear size={22} /> Ear Moulds
          </h1>
          <p className="text-[12px] text-slate-500 mt-0.5">
            Book custom ear moulds with an advance. Balance auto-lands in the patient&apos;s payment ledger.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={() => setShowBook(true)}
            data-testid="ha-em-book-btn"
            className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-semibold bg-indigo-600 hover:bg-indigo-700 text-white rounded shadow-sm"
          >
            <Plus size={13} /> Book Ear Mould
          </button>
          <button
            onClick={load}
            title="Reload"
            data-testid="ha-em-reload"
            className="p-1.5 text-slate-500 hover:text-slate-800 hover:bg-slate-100 rounded"
          >
            <RefreshCw size={14} />
          </button>
        </div>
      </div>

      {/* KPIs */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 mb-4">
        <Kpi label="Open Orders" value={kpis.total} testid="ha-em-kpi-open" />
        <Kpi label="Sent to Lab" value={kpis.sent} testid="ha-em-kpi-sent" tone="amber" />
        <Kpi label="Arrived — ready to collect" value={kpis.arrived} testid="ha-em-kpi-arrived" tone="indigo" />
        <Kpi label="Total Balance Due" value={fmtMoney(kpis.dueBalance)} testid="ha-em-kpi-due" tone="rose" />
      </div>

      {/* Filter row */}
      <div className="flex flex-wrap items-center gap-2 mb-3">
        <div className="relative flex-1 min-w-[220px]">
          <Search size={12} className="absolute left-2.5 top-2.5 text-slate-400 pointer-events-none" />
          <input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search by patient, order # or lab…"
            data-testid="ha-em-search"
            className="w-full pl-8 pr-3 py-1.5 text-xs border border-slate-300 rounded focus:outline-none focus:ring-2 focus:ring-indigo-200"
          />
        </div>
        <div className="flex items-center gap-1">
          {['', ...STATUS_ORDER].map((s) => (
            <button
              key={s || 'all'}
              onClick={() => setStatusFilter(s)}
              data-testid={`ha-em-filter-${s || 'all'}`}
              className={`px-2.5 py-1 text-[11px] font-semibold rounded border ${
                statusFilter === s
                  ? 'bg-slate-800 text-white border-slate-800'
                  : 'bg-white text-slate-700 border-slate-300 hover:bg-slate-50'
              }`}
            >
              {s ? STATUS_META[s].label : 'All'}
            </button>
          ))}
        </div>
      </div>

      {/* Table */}
      <div className="bg-white border border-slate-200 rounded-md overflow-x-auto">
        <table className="min-w-full text-sm">
          <thead className="bg-slate-50 border-b border-slate-200 text-[10px] uppercase tracking-widest text-slate-500 font-semibold">
            <tr>
              <th className="text-left px-3 py-2">Order #</th>
              <th className="text-left px-3 py-2">Patient</th>
              <th className="text-left px-3 py-2">Side</th>
              <th className="text-left px-3 py-2">Material / Specs</th>
              <th className="text-left px-3 py-2">Lab</th>
              <th className="text-left px-3 py-2">Expected</th>
              <th className="text-right px-3 py-2">Advance / Balance</th>
              <th className="text-left px-3 py-2">Status</th>
              <th className="px-3 py-2"></th>
            </tr>
          </thead>
          <tbody>
            {loading && (
              <tr><td colSpan={9} className="text-center py-8 text-slate-400 italic text-xs">Loading…</td></tr>
            )}
            {!loading && filtered.length === 0 && (
              <tr>
                <td colSpan={9} className="text-center py-10 text-slate-400 italic text-sm">
                  {search || statusFilter ? 'No orders match this filter.' : 'No ear-mould orders yet. Click Book Ear Mould to create one.'}
                </td>
              </tr>
            )}
            {filtered.map((r) => (
              <tr key={r.order_id} data-testid={`ha-em-row-${r.order_id}`} className="border-t border-slate-100 hover:bg-slate-50/50">
                <td className="px-3 py-2 font-mono text-[11px] font-semibold text-slate-800">{r.order_no}</td>
                <td className="px-3 py-2">
                  <Link
                    to={`/patients/${r.patient_id}?tab=payments`}
                    className="text-indigo-700 hover:underline font-semibold"
                  >
                    {r.patient_name || '—'}
                  </Link>
                  {r.patient_mobile && (
                    <div className="text-[10.5px] text-slate-500">{r.patient_mobile}</div>
                  )}
                </td>
                <td className="px-3 py-2 text-[12px]">{SIDE_LABEL[r.side] || r.side}</td>
                <td className="px-3 py-2 text-[12px] text-slate-700">
                  <div className="capitalize">{r.material || '—'}</div>
                  <div className="text-[10.5px] text-slate-500">
                    {r.side === 'both' && (r.vent_size_left || r.vent_size_right)
                      ? `Vent ${r.vent_size_left ? `L ${r.vent_size_left}` : ''}${r.vent_size_left && r.vent_size_right ? ' · ' : ''}${r.vent_size_right ? `R ${r.vent_size_right}` : ''}`
                      : (r.vent_size ? `Vent ${r.vent_size}` : '')}
                    {r.colour ? ` · ${r.colour}` : ''}
                  </div>
                </td>
                <td className="px-3 py-2 text-[12px]">{r.lab_vendor || <span className="text-slate-400 italic">—</span>}</td>
                <td className="px-3 py-2 text-[12px] tabular-nums">{fmtDay(r.expected_delivery_date)}</td>
                <td className="px-3 py-2 text-right text-[12px] tabular-nums">
                  <div className="text-emerald-700 font-semibold">{fmtMoney(r.advance_amount)}</div>
                  <div className={Number(r.balance_due) > 0 ? 'text-rose-700 font-semibold' : 'text-slate-400'}>
                    Bal: {fmtMoney(r.balance_due)}
                  </div>
                </td>
                <td className="px-3 py-2">
                  <StatusPicker order={r} onChanged={load} />
                </td>
                <td className="px-3 py-2 text-right whitespace-nowrap">
                  {r.invoice_id && (
                    <Link
                      to={`/billing/invoice/${r.invoice_id}`}
                      data-testid={`ha-em-invoice-link-${r.order_id}`}
                      className="text-[11px] font-semibold text-indigo-700 hover:underline"
                    >
                      Invoice →
                    </Link>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {showBook && (
        <BookEarMouldModal onClose={() => setShowBook(false)} onSaved={() => { setShowBook(false); load(); }} />
      )}
    </div>
  );
}

function Kpi({ label, value, testid, tone }) {
  const toneCls = tone === 'rose'   ? 'bg-rose-50 border-rose-200 text-rose-800'
                : tone === 'amber'  ? 'bg-amber-50 border-amber-200 text-amber-800'
                : tone === 'indigo' ? 'bg-indigo-50 border-indigo-200 text-indigo-800'
                : 'bg-white border-slate-200 text-slate-700';
  return (
    <div className={`rounded-md border px-3 py-2 ${toneCls}`} data-testid={testid}>
      <div className="text-[10px] uppercase tracking-widest font-semibold opacity-80">{label}</div>
      <div className="text-xl font-bold tabular-nums mt-0.5">{value}</div>
    </div>
  );
}

function StatusPicker({ order, onChanged }) {
  const [busy, setBusy] = useState(false);
  const meta = STATUS_META[order.status] || STATUS_META.pending_impression;

  // Native <select> — the browser handles collapse-on-outside-click,
  // keyboard navigation and mobile touch out of the box. Custom
  // absolute-positioned menus were overlapping the next row's badge,
  // making it un-clickable when a picker was already open.
  const change = async (e) => {
    const nextStatus = e.target.value;
    if (busy || !nextStatus || nextStatus === order.status) return;
    setBusy(true);
    try {
      await axios.patch(`${API}/ha/ear-moulds/${order.order_id}/status`, { status: nextStatus });
      onChanged?.();
    } finally {
      setBusy(false);
    }
  };

  return (
    <select
      value={order.status}
      onChange={change}
      disabled={busy}
      data-testid={`ha-em-status-${order.order_id}`}
      className={`cursor-pointer text-[10px] font-bold uppercase tracking-wider px-2 py-0.5 rounded border ${meta.tone} focus:outline-none focus:ring-2 focus:ring-indigo-200 disabled:opacity-50`}
    >
      <option value={order.status}>{meta.label}</option>
      {STATUS_ORDER.filter((s) => s !== order.status).map((s) => (
        <option key={s} value={s} data-testid={`ha-em-status-set-${order.order_id}-${s}`}>
          → {STATUS_META[s].label}
        </option>
      ))}
    </select>
  );
}

/* ============================================================
 *   BOOK EAR MOULD MODAL
 * ============================================================ */
function BookEarMouldModal({ onClose, onSaved }) {
  const [patientQ, setPatientQ] = useState('');
  const [patientOpts, setPatientOpts] = useState([]);
  const [patient, setPatient] = useState(null);

  const [side, setSide] = useState('both');
  const [material, setMaterial] = useState('silicone');
  const [vent, setVent] = useState('');
  const [ventLeft, setVentLeft] = useState('');
  const [ventRight, setVentRight] = useState('');
  const [colour, setColour] = useState('');
  const [lab, setLab] = useState('');
  const [expected, setExpected] = useState('');

  const [total, setTotal] = useState('');
  const [advance, setAdvance] = useState('');
  const [mode, setMode] = useState('cash');
  const [gst, setGst] = useState(18);
  const [notes, setNotes] = useState('');

  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');

  // Patient search — debounced.
  useEffect(() => {
    if (!patientQ.trim() || patient) { setPatientOpts([]); return; }
    const h = setTimeout(async () => {
      try {
        const r = await axios.get(`${API}/patients`, {
          params: { search: patientQ, limit: 8 },
        });
        const items = Array.isArray(r.data) ? r.data : (r.data?.items || []);
        setPatientOpts(items);
      } catch { /* ignore */ }
    }, 250);
    return () => clearTimeout(h);
  }, [patientQ, patient]);

  const balance = Math.max(0, Number(total || 0) - Number(advance || 0));
  const paymentStatus = Number(advance || 0) <= 0 ? 'unpaid'
    : Number(advance || 0) >= Number(total || 0) ? 'paid' : 'partial';

  const submit = async () => {
    setErr('');
    if (!patient) { setErr('Pick a patient'); return; }
    if (!total || Number(total) <= 0) { setErr('Enter the total amount'); return; }
    if (Number(advance || 0) > Number(total)) {
      setErr('Advance cannot exceed the total'); return;
    }
    setBusy(true);
    try {
      await axios.post(`${API}/ha/ear-moulds`, {
        patient_id: patient.patient_id,
        side,
        material,
        vent_size: side === 'both' ? null : (vent || null),
        vent_size_left: side === 'both' ? (ventLeft || null) : null,
        vent_size_right: side === 'both' ? (ventRight || null) : null,
        colour: colour || null,
        lab_vendor: lab || null,
        expected_delivery_date: expected || null,
        total_amount: Number(total),
        advance_amount: Number(advance || 0),
        payment_mode: mode,
        gst_rate: Number(gst || 0),
        notes: notes || null,
      });
      onSaved?.();
    } catch (e) {
      const d = e?.response?.data?.detail;
      setErr(typeof d === 'string' ? d : 'Booking failed');
      setBusy(false);
    }
  };

  return (
    <div
      onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}
      className="fixed inset-0 bg-black/40 z-40 flex items-start justify-center p-4 overflow-y-auto"
      data-testid="ha-em-book-modal"
    >
      <div className="bg-white rounded-lg shadow-xl w-full max-w-2xl my-6">
        <div className="flex items-center justify-between px-5 py-3 border-b border-slate-200">
          <div className="flex items-center gap-2">
            <Ear size={16} className="text-indigo-600" />
            <h2 className="text-base font-bold text-slate-800">Book Ear Mould</h2>
          </div>
          <button
            onClick={onClose}
            data-testid="ha-em-book-close"
            className="text-slate-400 hover:text-slate-800 text-lg leading-none"
          >×</button>
        </div>

        <div className="p-5 space-y-4">
          {/* Patient */}
          <div>
            <label className="text-[10px] uppercase tracking-widest text-slate-500 font-semibold mb-1 block">Patient *</label>
            {patient ? (
              <div
                data-testid="ha-em-book-patient"
                className="flex items-center justify-between bg-indigo-50 border border-indigo-200 rounded px-3 py-2"
              >
                <div>
                  <div className="text-[13px] font-semibold text-slate-800">{patient.name}</div>
                  <div className="text-[11px] text-slate-500 font-mono">{patient.patient_id}{patient.mobile ? ` · ${patient.mobile}` : ''}</div>
                </div>
                <button
                  onClick={() => { setPatient(null); setPatientQ(''); }}
                  className="text-[11px] text-slate-500 hover:text-slate-800"
                >Change</button>
              </div>
            ) : (
              <div className="relative">
                <input
                  value={patientQ}
                  onChange={(e) => setPatientQ(e.target.value)}
                  placeholder="Type patient name, phone or MRD…"
                  data-testid="ha-em-book-patient-search"
                  className="w-full px-3 py-1.5 text-xs border border-slate-300 rounded focus:outline-none focus:border-indigo-500"
                />
                {patientOpts.length > 0 && (
                  <div className="absolute top-full mt-1 w-full bg-white border border-slate-200 rounded shadow-lg z-10 max-h-56 overflow-y-auto">
                    {patientOpts.map((p) => (
                      <button
                        key={p.patient_id}
                        onClick={() => { setPatient(p); setPatientOpts([]); }}
                        data-testid={`ha-em-book-patient-opt-${p.patient_id}`}
                        className="w-full text-left px-3 py-2 text-[12px] hover:bg-slate-50"
                      >
                        <div className="font-semibold text-slate-800">{p.name}</div>
                        <div className="text-[10.5px] text-slate-500">
                          {p.mobile || '—'} · <span className="font-mono">{p.patient_id}</span>
                        </div>
                      </button>
                    ))}
                  </div>
                )}
              </div>
            )}
          </div>

          {/* Phase 2B.3 · Advance-availability alert. Informational. */}
          {patient?.patient_id && (
            <PatientAdvancesBanner patientId={patient.patient_id} />
          )}

          {/* Side + material row */}
          <div className="grid grid-cols-3 gap-3">
            <div>
              <label className="text-[10px] uppercase tracking-widest text-slate-500 font-semibold mb-1 block">Side *</label>
              <div className="flex gap-1">
                {['left', 'right', 'both'].map((s) => (
                  <button
                    key={s}
                    type="button"
                    onClick={() => setSide(s)}
                    data-testid={`ha-em-book-side-${s}`}
                    className={`flex-1 text-[11px] font-semibold py-1.5 rounded border ${
                      side === s
                        ? 'bg-indigo-600 text-white border-indigo-600'
                        : 'bg-white text-slate-700 border-slate-300 hover:bg-slate-50'
                    }`}
                  >{SIDE_LABEL[s]}</button>
                ))}
              </div>
            </div>
            <div>
              <label className="text-[10px] uppercase tracking-widest text-slate-500 font-semibold mb-1 block">Material</label>
              <select
                value={material}
                onChange={(e) => setMaterial(e.target.value)}
                data-testid="ha-em-book-material"
                className="w-full px-2 py-1.5 text-xs border border-slate-300 rounded"
              >
                <option value="silicone">Silicone</option>
                <option value="acrylic">Acrylic (hard)</option>
                <option value="soft_acrylic">Soft Acrylic</option>
                <option value="vinyl">Vinyl</option>
                <option value="polyethylene">Polyethylene</option>
              </select>
            </div>
            {side !== 'both' ? (
              <div>
                <label className="text-[10px] uppercase tracking-widest text-slate-500 font-semibold mb-1 block">Vent size</label>
                <input
                  value={vent}
                  onChange={(e) => setVent(e.target.value)}
                  placeholder="e.g. 1.5mm"
                  data-testid="ha-em-book-vent"
                  className="w-full px-2 py-1.5 text-xs border border-slate-300 rounded"
                />
              </div>
            ) : (
              <div>
                <label className="text-[10px] uppercase tracking-widest text-slate-500 font-semibold mb-1 block">Vent (L / R)</label>
                <div className="flex gap-1">
                  <input
                    value={ventLeft}
                    onChange={(e) => setVentLeft(e.target.value)}
                    placeholder="Left e.g. 1.5mm"
                    data-testid="ha-em-book-vent-left"
                    className="w-1/2 px-2 py-1.5 text-xs border border-slate-300 rounded"
                  />
                  <input
                    value={ventRight}
                    onChange={(e) => setVentRight(e.target.value)}
                    placeholder="Right e.g. IROS"
                    data-testid="ha-em-book-vent-right"
                    className="w-1/2 px-2 py-1.5 text-xs border border-slate-300 rounded"
                  />
                </div>
              </div>
            )}
          </div>

          {/* Colour + Lab + Expected */}
          <div className="grid grid-cols-3 gap-3">
            <div>
              <label className="text-[10px] uppercase tracking-widest text-slate-500 font-semibold mb-1 block">Colour</label>
              <input
                value={colour}
                onChange={(e) => setColour(e.target.value)}
                placeholder="Skin / Beige / Clear…"
                data-testid="ha-em-book-colour"
                className="w-full px-2 py-1.5 text-xs border border-slate-300 rounded"
              />
            </div>
            <div>
              <label className="text-[10px] uppercase tracking-widest text-slate-500 font-semibold mb-1 block">Lab vendor</label>
              <input
                value={lab}
                onChange={(e) => setLab(e.target.value)}
                placeholder="Lab name (optional)"
                data-testid="ha-em-book-lab"
                className="w-full px-2 py-1.5 text-xs border border-slate-300 rounded"
              />
            </div>
            <div>
              <label className="text-[10px] uppercase tracking-widest text-slate-500 font-semibold mb-1 block flex items-center gap-1">
                <Calendar size={11} /> Expected on
              </label>
              <input
                type="date"
                value={expected}
                onChange={(e) => setExpected(e.target.value)}
                data-testid="ha-em-book-expected"
                className="w-full px-2 py-1.5 text-xs border border-slate-300 rounded"
              />
            </div>
          </div>

          {/* Money block */}
          <div className="grid grid-cols-4 gap-3 p-3 bg-slate-50 border border-slate-200 rounded">
            <div>
              <label className="text-[10px] uppercase tracking-widest text-slate-500 font-semibold mb-1 block">Total (₹) *</label>
              <input
                type="number"
                min="0"
                value={total}
                onChange={(e) => setTotal(e.target.value)}
                data-testid="ha-em-book-total"
                className="w-full px-2 py-1.5 text-xs border border-slate-300 rounded"
              />
            </div>
            <div>
              <label className="text-[10px] uppercase tracking-widest text-slate-500 font-semibold mb-1 block">Advance (₹)</label>
              <input
                type="number"
                min="0"
                value={advance}
                onChange={(e) => setAdvance(e.target.value)}
                data-testid="ha-em-book-advance"
                className="w-full px-2 py-1.5 text-xs border border-slate-300 rounded"
              />
            </div>
            <div>
              <label className="text-[10px] uppercase tracking-widest text-slate-500 font-semibold mb-1 block">Mode</label>
              <select
                value={mode}
                onChange={(e) => setMode(e.target.value)}
                data-testid="ha-em-book-mode"
                className="w-full px-2 py-1.5 text-xs border border-slate-300 rounded"
              >
                <option value="cash">Cash</option>
                <option value="upi">UPI</option>
                <option value="card">Card</option>
                <option value="bank">Bank</option>
                <option value="cheque">Cheque</option>
                <option value="other">Other</option>
              </select>
            </div>
            <div>
              <label className="text-[10px] uppercase tracking-widest text-slate-500 font-semibold mb-1 block">GST %</label>
              <input
                type="number"
                min="0"
                value={gst}
                onChange={(e) => setGst(e.target.value)}
                data-testid="ha-em-book-gst"
                className="w-full px-2 py-1.5 text-xs border border-slate-300 rounded"
              />
            </div>
            <div className="col-span-4 flex flex-wrap justify-between gap-2 text-[11.5px] pt-1 border-t border-slate-200">
              <span>Balance due <b className={balance > 0 ? 'text-rose-700' : 'text-slate-500'}>{fmtMoney(balance)}</b></span>
              <span className={`font-semibold ${paymentStatus === 'paid' ? 'text-emerald-700' : paymentStatus === 'partial' ? 'text-amber-800' : 'text-rose-700'}`}>
                Invoice → {paymentStatus.toUpperCase()}
              </span>
            </div>
          </div>

          <div>
            <label className="text-[10px] uppercase tracking-widest text-slate-500 font-semibold mb-1 block">Notes</label>
            <textarea
              value={notes}
              onChange={(e) => setNotes(e.target.value)}
              rows={2}
              placeholder="Special requests, patient preferences, etc."
              data-testid="ha-em-book-notes"
              className="w-full px-2 py-1.5 text-xs border border-slate-300 rounded"
            />
          </div>

          {err && (
            <div className="text-xs text-rose-700 bg-rose-50 border border-rose-200 rounded px-3 py-2">
              {err}
            </div>
          )}
        </div>

        <div className="flex items-center justify-end gap-2 px-5 py-3 border-t border-slate-200 bg-slate-50">
          <button
            onClick={onClose}
            className="px-3 py-1.5 text-xs font-semibold text-slate-600 hover:bg-slate-200 rounded"
          >Cancel</button>
          <button
            onClick={submit}
            disabled={busy || !patient || !total}
            data-testid="ha-em-book-submit"
            className="inline-flex items-center gap-1.5 px-4 py-1.5 text-xs font-semibold bg-indigo-600 hover:bg-indigo-700 disabled:bg-slate-300 text-white rounded shadow-sm"
          >
            <Package size={12} /> {busy ? 'Booking…' : 'Book & Generate Invoice'}
          </button>
        </div>
      </div>
    </div>
  );
}
