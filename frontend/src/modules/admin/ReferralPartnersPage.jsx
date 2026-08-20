/**
 * M12 Referral Partners — Admin View (Phase 13.C)
 * List + create + activate/suspend partners. View stats & create payouts.
 */
import React, { useEffect, useState } from 'react';
import axios from 'axios';

const API = `${process.env.REACT_APP_BACKEND_URL}/api`;
const fmtINR = (n) => `₹${Number(n || 0).toLocaleString('en-IN')}`;
const fmtDate = (iso) => (iso ? new Date(iso).toLocaleDateString('en-IN', { day: '2-digit', month: 'short', year: 'numeric' }) : '—');

export default function ReferralPartnersPage() {
  const [partners, setPartners] = useState([]);
  const [err, setErr] = useState('');
  const [showForm, setShowForm] = useState(false);
  const [selected, setSelected] = useState(null);

  const load = async () => {
    setErr('');
    try {
      const r = await axios.get(`${API}/referral-partners`);
      setPartners(r.data || []);
    } catch (e) {
      setErr(e?.response?.data?.detail?.message || e?.response?.data?.detail || 'Failed to load');
    }
  };
  useEffect(() => { load(); }, []);

  const activate = async (p) => {
    await axios.patch(`${API}/referral-partners/${p.partner_id}`, { status: 'active' });
    load();
  };
  const suspend = async (p) => {
    await axios.patch(`${API}/referral-partners/${p.partner_id}`, { status: 'suspended' });
    load();
  };

  return (
    <div className="p-6 space-y-5" data-testid="partners-admin-page">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-slate-900">Referral Partners</h1>
          <p className="text-sm text-slate-500 mt-0.5">External referrers who send patients to your clinic · <span className="text-indigo-700 font-semibold">M12</span></p>
        </div>
        <button data-testid="partner-new-btn" onClick={() => setShowForm(true)} className="px-3 py-1.5 text-xs font-semibold text-white bg-indigo-600 hover:bg-indigo-700 rounded-md shadow">+ Add Partner</button>
      </div>

      {err && <div className="text-xs text-rose-700 bg-rose-50 border border-rose-200 rounded px-3 py-2">{err}</div>}

      <div className="bg-white rounded-lg border border-slate-200 overflow-hidden">
        <table className="w-full text-sm" data-testid="partners-table">
          <thead className="bg-slate-50 text-xs uppercase tracking-wider text-slate-500">
            <tr>
              <th className="px-4 py-2 text-left">Partner</th>
              <th className="px-4 py-2 text-left">Code</th>
              <th className="px-4 py-2 text-left">Org</th>
              <th className="px-4 py-2 text-left">Commission</th>
              <th className="px-4 py-2 text-left">Since</th>
              <th className="px-4 py-2 text-center">Status</th>
              <th className="px-4 py-2 text-right">Actions</th>
            </tr>
          </thead>
          <tbody>
            {partners.map((p) => (
              <tr key={p.partner_id} className="border-t border-slate-100 hover:bg-slate-50">
                <td className="px-4 py-2">
                  <div className="font-semibold">{p.name}</div>
                  <div className="text-[10px] text-slate-500">{p.email} · {p.phone || ''}</div>
                </td>
                <td className="px-4 py-2 font-mono text-xs text-indigo-700">{p.referral_code}</td>
                <td className="px-4 py-2 text-xs">{p.organization || '—'}</td>
                <td className="px-4 py-2 text-xs">
                  {p.commission_kind === 'percent' ? `${p.commission_value}%` : fmtINR(p.commission_value) + ' / ref'}
                </td>
                <td className="px-4 py-2 text-xs">{fmtDate(p.partner_since)}</td>
                <td className="px-4 py-2 text-center">
                  <span className={`px-2 py-0.5 rounded-full text-[10px] font-semibold uppercase tracking-wider ${
                    p.status === 'active' ? 'bg-emerald-100 text-emerald-700' :
                    p.status === 'pending' ? 'bg-amber-100 text-amber-700' :
                    'bg-rose-100 text-rose-700'
                  }`}>{p.status}</span>
                </td>
                <td className="px-4 py-2 text-right space-x-2">
                  <button data-testid={`partner-view-${p.partner_id}`} onClick={() => setSelected(p)} className="text-xs text-indigo-600 hover:underline">Stats</button>
                  {p.status === 'pending' && <button onClick={() => activate(p)} className="text-xs text-emerald-700 hover:underline">Approve</button>}
                  {p.status === 'active' && <button onClick={() => suspend(p)} className="text-xs text-rose-600 hover:underline">Suspend</button>}
                </td>
              </tr>
            ))}
            {partners.length === 0 && <tr><td colSpan={7} className="text-center py-10 text-slate-500 text-sm">No partners yet. Add one to start tracking referrals.</td></tr>}
          </tbody>
        </table>
      </div>

      {showForm && <PartnerForm onClose={() => setShowForm(false)} onSaved={() => { setShowForm(false); load(); }} />}
      {selected && <PartnerStatsDrawer partner={selected} onClose={() => setSelected(null)} />}
    </div>
  );
}

const PartnerForm = ({ onClose, onSaved }) => {
  const [f, setF] = useState({
    name: '', email: '', phone: '', organization: '', specialization: '', city: '',
    commission_kind: 'percent', commission_value: 5, password: '',
  });
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');

  const submit = async (e) => {
    e.preventDefault();
    setBusy(true); setErr('');
    try {
      await axios.post(`${API}/referral-partners`, { ...f, commission_value: parseFloat(f.commission_value) });
      onSaved();
    } catch (e) {
      setErr(e?.response?.data?.detail?.message || e?.response?.data?.detail || 'Failed');
    } finally { setBusy(false); }
  };

  return (
    <div className="fixed inset-0 bg-slate-900/50 flex items-center justify-center z-40 p-4">
      <form onSubmit={submit} className="bg-white rounded-xl shadow-xl p-6 w-full max-w-lg space-y-3" data-testid="partner-form">
        <h2 className="text-lg font-bold text-slate-900">Add Referral Partner</h2>
        <div className="grid grid-cols-2 gap-3">
          <Input label="Name" value={f.name} onChange={(v) => setF({ ...f, name: v })} required testid="partner-name" />
          <Input label="Email" type="email" value={f.email} onChange={(v) => setF({ ...f, email: v })} required testid="partner-email" />
        </div>
        <div className="grid grid-cols-2 gap-3">
          <Input label="Phone" value={f.phone} onChange={(v) => setF({ ...f, phone: v })} />
          <Input label="City" value={f.city} onChange={(v) => setF({ ...f, city: v })} />
        </div>
        <div className="grid grid-cols-2 gap-3">
          <Input label="Organisation" value={f.organization} onChange={(v) => setF({ ...f, organization: v })} />
          <Input label="Specialisation" value={f.specialization} onChange={(v) => setF({ ...f, specialization: v })} />
        </div>
        <div className="grid grid-cols-2 gap-3">
          <label className="block">
            <span className="text-[11px] font-semibold text-slate-600 uppercase tracking-wider">Commission kind</span>
            <select value={f.commission_kind} onChange={(e) => setF({ ...f, commission_kind: e.target.value })} className="mt-0.5 w-full px-2 py-1.5 text-sm border border-slate-300 rounded">
              <option value="percent">Percent of revenue</option>
              <option value="fixed">Fixed ₹ per referral</option>
            </select>
          </label>
          <Input label={f.commission_kind === 'percent' ? 'Commission %' : 'Commission ₹'} type="number" value={f.commission_value} onChange={(v) => setF({ ...f, commission_value: v })} />
        </div>
        <Input label="Portal password (optional — enables partner login)" type="password" value={f.password} onChange={(v) => setF({ ...f, password: v })} testid="partner-password" />
        {err && <div className="text-xs text-rose-700">{err}</div>}
        <div className="flex justify-end gap-2 pt-2">
          <button type="button" onClick={onClose} className="px-3 py-1.5 text-xs font-semibold text-slate-600">Cancel</button>
          <button disabled={busy} className="px-3 py-1.5 text-xs font-semibold text-white bg-indigo-600 hover:bg-indigo-700 rounded disabled:opacity-50" data-testid="partner-save">{busy ? 'Saving…' : 'Create Partner'}</button>
        </div>
      </form>
    </div>
  );
};

const Input = ({ label, value, onChange, type = 'text', required, testid }) => (
  <label className="block">
    <span className="text-[11px] font-semibold text-slate-600 uppercase tracking-wider">{label}{required && <span className="text-rose-600"> *</span>}</span>
    <input data-testid={testid} type={type} value={value || ''} onChange={(e) => onChange(e.target.value)} required={required} className="mt-0.5 w-full px-2 py-1.5 text-sm border border-slate-300 rounded focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500 outline-none" />
  </label>
);

const PartnerStatsDrawer = ({ partner, onClose }) => {
  const [stats, setStats] = useState(null);
  const [payouts, setPayouts] = useState([]);
  const [busy, setBusy] = useState(false);
  const [showPayout, setShowPayout] = useState(false);

  const load = async () => {
    const [s, p] = await Promise.all([
      axios.get(`${API}/referral-partners/${partner.partner_id}/stats?days=90`),
      axios.get(`${API}/referral-partners/${partner.partner_id}/payouts`),
    ]);
    setStats(s.data); setPayouts(p.data || []);
  };
  useEffect(() => { load(); }, [partner.partner_id]);

  const markPaid = async (po) => {
    setBusy(true);
    try {
      await axios.post(`${API}/referral-partners/${partner.partner_id}/payouts/${po.payout_id}/mark-paid`, { payment_ref: prompt('Payment reference (UTR/UPI txn id):') || '' });
      load();
    } finally { setBusy(false); }
  };

  return (
    <div className="fixed inset-0 bg-slate-900/50 flex items-end md:items-center justify-center z-40 p-4">
      <div className="bg-white rounded-xl shadow-xl w-full max-w-2xl max-h-[90vh] flex flex-col" data-testid="partner-stats-drawer">
        <div className="px-5 py-3 border-b border-slate-200 flex items-center justify-between">
          <div>
            <h2 className="text-lg font-bold">{partner.name}</h2>
            <p className="text-xs text-slate-500">{partner.email} · Code <span className="font-mono text-indigo-700">{partner.referral_code}</span></p>
          </div>
          <button onClick={onClose} className="text-slate-400 hover:text-slate-700 text-2xl leading-none">×</button>
        </div>
        <div className="p-5 space-y-4 overflow-auto">
          {stats && (
            <div className="grid grid-cols-4 gap-2">
              <MiniTile label="Patients" v={stats.stats.patients} />
              <MiniTile label="Revenue" v={fmtINR(stats.stats.total_revenue)} />
              <MiniTile label="Est. Commission" v={fmtINR(stats.stats.commission_estimate)} />
              <MiniTile label="Window" v={`${stats.window_days}d`} />
            </div>
          )}

          <div className="flex items-center justify-between">
            <h3 className="text-sm font-bold text-slate-700">Payout history</h3>
            <button
              type="button"
              onClick={() => !showPayout && setShowPayout(true)}
              disabled={busy || showPayout}
              className="text-xs font-semibold text-indigo-700 hover:underline disabled:opacity-50 disabled:cursor-not-allowed"
              data-testid="partner-new-payout-btn"
            >+ Create payout</button>
          </div>
          <table className="w-full text-xs border border-slate-200 rounded">
            <thead className="bg-slate-50 text-[10px] uppercase tracking-wider text-slate-500">
              <tr>
                <th className="px-3 py-2 text-left">ID</th>
                <th className="px-3 py-2 text-left">Period</th>
                <th className="px-3 py-2 text-right">Refs</th>
                <th className="px-3 py-2 text-right">Revenue</th>
                <th className="px-3 py-2 text-right">Commission</th>
                <th className="px-3 py-2 text-center">Status</th>
                <th className="px-3 py-2"></th>
              </tr>
            </thead>
            <tbody>
              {payouts.map((p) => (
                <tr key={p.payout_id} className="border-t border-slate-100">
                  <td className="px-3 py-2 font-mono text-indigo-700">{p.payout_id}</td>
                  <td className="px-3 py-2">{p.period_start} → {p.period_end}</td>
                  <td className="px-3 py-2 text-right">{p.referral_count}</td>
                  <td className="px-3 py-2 text-right">{fmtINR(p.attributed_revenue)}</td>
                  <td className="px-3 py-2 text-right font-bold">{fmtINR(p.commission_amount)}</td>
                  <td className="px-3 py-2 text-center">
                    <span className={`px-2 py-0.5 rounded-full text-[10px] font-semibold ${p.status === 'paid' ? 'bg-emerald-100 text-emerald-700' : 'bg-amber-100 text-amber-700'}`}>{p.status}</span>
                  </td>
                  <td className="px-3 py-2 text-right">
                    {p.status === 'pending' && <button disabled={busy} onClick={() => markPaid(p)} className="text-[11px] text-emerald-700 hover:underline">Mark paid</button>}
                  </td>
                </tr>
              ))}
              {payouts.length === 0 && <tr><td colSpan={7} className="text-center py-6 text-slate-500">No payouts yet.</td></tr>}
            </tbody>
          </table>
        </div>
      </div>
      {showPayout && <PayoutForm partnerId={partner.partner_id} onClose={() => setShowPayout(false)} onSaved={() => { setShowPayout(false); load(); }} />}
    </div>
  );
};

const MiniTile = ({ label, v }) => (
  <div className="bg-slate-50 rounded p-2 text-center">
    <div className="text-[9px] uppercase tracking-wider text-slate-500 font-semibold">{label}</div>
    <div className="text-sm font-bold text-slate-900 mt-0.5">{v}</div>
  </div>
);

const PayoutForm = ({ partnerId, onClose, onSaved }) => {
  const today = new Date().toISOString().slice(0, 10);
  const monthAgo = new Date(Date.now() - 30 * 86400000).toISOString().slice(0, 10);
  const [start, setStart] = useState(monthAgo);
  const [end, setEnd] = useState(today);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');

  const submit = async (e) => {
    e.preventDefault();
    setBusy(true); setErr('');
    try {
      await axios.post(`${API}/referral-partners/${partnerId}/payouts`, { period_start: start, period_end: end });
      onSaved();
    } catch (e) {
      setErr(e?.response?.data?.detail?.message || 'Failed');
    } finally { setBusy(false); }
  };

  return (
    <div className="fixed inset-0 bg-slate-900/50 flex items-center justify-center z-50 p-4">
      <form onSubmit={submit} className="bg-white rounded-xl shadow-xl p-6 w-full max-w-md space-y-3">
        <h3 className="text-base font-bold">Create payout</h3>
        <Input label="Period start" type="date" value={start} onChange={setStart} required />
        <Input label="Period end" type="date" value={end} onChange={setEnd} required />
        {err && <div className="text-xs text-rose-700">{err}</div>}
        <div className="flex justify-end gap-2">
          <button type="button" onClick={onClose} className="px-3 py-1.5 text-xs text-slate-600">Cancel</button>
          <button disabled={busy} className="px-3 py-1.5 text-xs font-semibold text-white bg-indigo-600 rounded">Create</button>
        </div>
      </form>
    </div>
  );
};
