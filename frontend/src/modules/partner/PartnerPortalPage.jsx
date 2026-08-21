/**
 * Partner Self-Portal (Phase 13.C)
 * Lands here when a user with role=referral_partner logs in.
 * Shows dashboard: referred patients, earnings, payouts.
 */
import React, { useEffect, useState } from 'react';
import axios from 'axios';
import { useNavigate } from 'react-router-dom';
import { useAuth } from '../../AuthContext';

const API = `${process.env.REACT_APP_BACKEND_URL}/api`;
const fmtINR = (n) => `₹${Number(n || 0).toLocaleString('en-IN')}`;
const fmtDate = (iso) => (iso ? new Date(iso).toLocaleDateString('en-IN', { day: '2-digit', month: 'short', year: 'numeric' }) : '—');

export default function PartnerPortalPage() {
  const [data, setData] = useState(null);
  const [err, setErr] = useState('');
  const [days, setDays] = useState(90);
  const { user, logout } = useAuth();
  const navigate = useNavigate();

  const load = async () => {
    setErr('');
    try {
      const r = await axios.get(`${API}/referral-partners/me/dashboard?days=${days}`);
      setData(r.data);
    } catch (e) {
      setErr(e?.response?.data?.detail?.message || e?.response?.data?.detail || 'Failed to load partner dashboard');
    }
  };
  useEffect(() => { load(); }, [days]);

  if (err) return (
    <div className="p-10 text-center text-sm text-rose-700 bg-rose-50 border border-rose-200 rounded m-8">
      {err}
      <button onClick={() => { logout(); navigate('/login'); }} className="ml-3 underline text-rose-900">Sign out</button>
    </div>
  );
  if (!data) return <div className="p-10 text-center text-slate-500">Loading your referral dashboard…</div>;

  const { partner, stats, recent_patients, payouts, status_message } = data;

  return (
    <div className="min-h-screen bg-slate-100" data-testid="partner-portal-page">
      <header className="bg-slate-900 text-white px-6 py-4 flex items-center justify-between">
        <div>
          <div className="text-xs uppercase tracking-[0.2em] text-indigo-300">Partner Portal</div>
          <div className="text-lg font-bold">{partner.name}</div>
        </div>
        <div className="flex items-center gap-3">
          <div className="text-xs text-right">
            <div className="text-slate-300">Your code</div>
            <div className="font-mono text-indigo-300 font-bold text-base">{partner.referral_code}</div>
          </div>
          <button onClick={() => { logout(); navigate('/login'); }} className="text-xs px-3 py-1.5 bg-slate-800 hover:bg-slate-700 rounded" data-testid="partner-logout-btn">Sign out</button>
        </div>
      </header>

      <main className="p-6 space-y-5 max-w-5xl mx-auto">
        {status_message && (
          <div className="bg-amber-50 border border-amber-200 text-amber-900 text-sm rounded-lg px-4 py-3">
            {status_message}
          </div>
        )}

        <div className="flex items-center justify-between">
          <h1 className="text-2xl font-bold text-slate-900">Your Referrals Dashboard</h1>
          <select value={days} onChange={(e) => setDays(parseInt(e.target.value))} className="text-xs px-2 py-1.5 border border-slate-300 rounded" data-testid="partner-window">
            <option value={30}>Last 30 days</option>
            <option value={90}>Last 90 days</option>
            <option value={365}>Last 1 year</option>
          </select>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-4 gap-3">
          <Tile label="Patients referred" v={stats.patients} tone="indigo" />
          <Tile label="Revenue generated" v={fmtINR(stats.total_revenue)} tone="emerald" />
          <Tile label="Est. commission" v={fmtINR(stats.commission_estimate || 0)} tone="amber" />
          <Tile label={partner.commission_kind === 'percent' ? 'Your rate' : 'Your fixed fee'} v={partner.commission_kind === 'percent' ? `${partner.commission_value}%` : fmtINR(partner.commission_value)} tone="slate" />
        </div>

        {/* NAV-011 · Phase 2C · Category-aware attribution.
            Analytics-only view — the payout above is unaffected. */}
        <div className="grid grid-cols-1 md:grid-cols-3 gap-3" data-testid="partner-category-attribution">
          <Tile
            label="Diagnostics revenue"
            v={fmtINR(stats.diagnostics_revenue || 0)}
            tone="sky"
            testid="partner-diagnostics-revenue"
          />
          <Tile
            label="Hearing Aid / Core Business revenue"
            v={fmtINR(stats.ha_sales_revenue || 0)}
            tone="violet"
            testid="partner-ha-sales-revenue"
          />
          <Tile
            label="Total attributed revenue"
            v={fmtINR(stats.total_attributed_revenue || 0)}
            tone="emerald"
            testid="partner-total-attributed-revenue"
          />
        </div>

        <div className="grid md:grid-cols-2 gap-5">
          <div className="bg-white border border-slate-200 rounded-lg overflow-hidden" data-testid="partner-patients-card">
            <div className="px-4 py-2 bg-slate-50 border-b text-xs font-bold uppercase tracking-wider text-slate-700">Recent Referred Patients</div>
            <ul className="divide-y divide-slate-100 max-h-72 overflow-auto">
              {recent_patients.map((p) => (
                <li key={p.patient_id} className="px-4 py-2 text-sm flex items-center justify-between">
                  <span className="font-semibold">{p.display_name}</span>
                  <span className="text-xs text-slate-500">{fmtDate(p.created_at)} · {p.city || ''}</span>
                </li>
              ))}
              {recent_patients.length === 0 && <li className="px-4 py-6 text-center text-sm text-slate-500">No referrals yet. Share your code with patients!</li>}
            </ul>
          </div>

          <div className="bg-white border border-slate-200 rounded-lg overflow-hidden" data-testid="partner-payouts-card">
            <div className="px-4 py-2 bg-slate-50 border-b text-xs font-bold uppercase tracking-wider text-slate-700">Payout History</div>
            <table className="w-full text-xs">
              <thead className="text-[10px] uppercase text-slate-500 bg-slate-50">
                <tr>
                  <th className="px-3 py-1.5 text-left">ID</th>
                  <th className="px-3 py-1.5 text-left">Period</th>
                  <th className="px-3 py-1.5 text-right">Commission</th>
                  <th className="px-3 py-1.5 text-center">Status</th>
                </tr>
              </thead>
              <tbody>
                {payouts.map((p) => (
                  <tr key={p.payout_id} className="border-t border-slate-100">
                    <td className="px-3 py-1.5 font-mono text-indigo-700">{p.payout_id}</td>
                    <td className="px-3 py-1.5">{p.period_start}</td>
                    <td className="px-3 py-1.5 text-right font-bold">{fmtINR(p.commission_amount)}</td>
                    <td className="px-3 py-1.5 text-center">
                      <span className={`px-2 py-0.5 rounded-full text-[10px] font-semibold ${p.status === 'paid' ? 'bg-emerald-100 text-emerald-700' : 'bg-amber-100 text-amber-700'}`}>{p.status}</span>
                    </td>
                  </tr>
                ))}
                {payouts.length === 0 && <tr><td colSpan={4} className="text-center py-6 text-slate-500">No payouts yet.</td></tr>}
              </tbody>
            </table>
          </div>
        </div>

        <div className="bg-indigo-50 border border-indigo-200 rounded-lg p-5 text-sm text-indigo-900">
          <div className="font-bold mb-1">Share your referral code</div>
          <p className="text-xs">Ask your patients to mention <span className="font-mono font-bold bg-white px-1.5 py-0.5 rounded border border-indigo-300">{partner.referral_code}</span> at the front desk when they visit. Your commission is tracked automatically.</p>
        </div>
      </main>
    </div>
  );
}

const Tile = ({ label, v, tone, testid }) => {
  const colorMap = {
    indigo: 'bg-indigo-50 border-indigo-200 text-indigo-900',
    emerald: 'bg-emerald-50 border-emerald-200 text-emerald-900',
    amber: 'bg-amber-50 border-amber-200 text-amber-900',
    sky: 'bg-sky-50 border-sky-200 text-sky-900',
    violet: 'bg-violet-50 border-violet-200 text-violet-900',
    slate: 'bg-white border-slate-200 text-slate-900',
  }[tone] || 'bg-slate-50 border-slate-200';
  return (
    <div className={`rounded-lg p-4 border ${colorMap}`} data-testid={testid}>
      <div className="text-[10px] font-semibold uppercase tracking-wider opacity-75">{label}</div>
      <div className="text-2xl font-bold mt-1">{v}</div>
    </div>
  );
};
