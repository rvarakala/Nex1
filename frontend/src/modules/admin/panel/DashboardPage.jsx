import React, { useEffect, useState } from 'react';
import axios from 'axios';
import { Link } from 'react-router-dom';
import { ResponsiveContainer, AreaChart, Area, LineChart, Line, BarChart, Bar, PieChart, Pie, Cell, XAxis, YAxis, Tooltip, CartesianGrid, Legend } from 'recharts';
import { AlertTriangle } from 'lucide-react';
import { PageHeader, Card, KPITile, Pill, tierTone, fmtINR, fmtInt, fmtDate, Empty } from './shared';
import LiveSignupPulse from './LiveSignupPulse';
import EmailHealthBanner from './EmailHealthBanner';
import WebhookHealthBanner from './WebhookHealthBanner';
import SignupFunnel from './SignupFunnel';
import LatencySpeedometer from './LatencySpeedometer';
import FounderResetModal from './FounderResetModal';
import LaunchBannerAdminCard from './LaunchBannerAdminCard';
import { useAuth } from '../../../AuthContext';

const API = `${process.env.REACT_APP_BACKEND_URL}/api`;
const PIE_COLORS = ['#94a3b8', '#6366f1', '#d946ef'];

export default function DashboardPage() {
  const [d, setD] = useState(null);
  const [err, setErr] = useState('');
  const [showReset, setShowReset] = useState(false);
  const { user } = useAuth();

  const load = () => {
    axios.get(`${API}/admin/v2/dashboard`)
      .then((r) => setD(r.data))
      .catch((e) => setErr(e?.response?.data?.detail?.message || e?.response?.data?.detail || 'Failed to load dashboard'));
  };
  useEffect(() => { load(); }, []);

  if (err) return <div className="p-6"><div className="text-sm text-rose-700 bg-rose-50 border border-rose-200 rounded p-3">{err}</div></div>;
  if (!d) return <div className="p-6 text-slate-500">Loading command center…</div>;

  const { kpis, plan_distribution, revenue_by_tier, mrr_chart, signups_trend, funnel, signup_funnel_30d, recent_signups, renewals_due } = d;

  return (
    <div className="p-6 space-y-6" data-testid="admin-dashboard-page">
      <div className="flex items-center justify-between gap-4 flex-wrap">
        <PageHeader title="Executive Dashboard" subtitle="Platform-wide health, revenue & growth" />
        <div className="flex items-center gap-2">
          {user?.role === 'founder' && (
            <button
              onClick={() => setShowReset(true)}
              data-testid="founder-reset-btn"
              title="Wipe leads + test clinics + revenue baseline (founder only)"
              className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-semibold text-rose-700 bg-rose-50 hover:bg-rose-100 border border-rose-200 rounded"
            >
              <AlertTriangle size={12} /> Reset Test Data
            </button>
          )}
          <LiveSignupPulse />
        </div>
      </div>

      {showReset && (
        <FounderResetModal
          onClose={() => setShowReset(false)}
          onDone={() => { load(); }}
        />
      )}

      <EmailHealthBanner />
      <WebhookHealthBanner />

      {/* KPI row — 8 tiles only at 2xl (1536px+) so currency values don't ellipsis-truncate on 1280-1440 desktops */}
      <div className="grid grid-cols-2 md:grid-cols-4 2xl:grid-cols-8 gap-3">
        <KPITile label="Active Clinics" value={fmtInt(kpis.active_clinics)} tone="emerald" testid="kpi-active" />
        <KPITile label="On Trial" value={fmtInt(kpis.trial_accounts)} tone="indigo" testid="kpi-trials" />
        <KPITile label="MRR" value={fmtINR(kpis.mrr)} tone="fuchsia" testid="kpi-mrr" />
        <KPITile label="ARR" value={fmtINR(kpis.arr)} tone="fuchsia" testid="kpi-arr" />
        <KPITile label="New 30d" value={fmtInt(kpis.new_signups_30d)} tone="emerald" testid="kpi-new-signups" />
        <KPITile label="Churn %" value={`${kpis.churn_rate_pct}%`} tone="rose" testid="kpi-churn" />
        <KPITile label="Payment Fails" value={fmtInt(kpis.payment_failures)} tone="amber" testid="kpi-pay-fail" />
        <KPITile label="Avg ₹ / Tenant" value={fmtINR(kpis.avg_revenue_per_tenant)} tone="slate" testid="kpi-arpt" />
      </div>

      <SignupFunnel data={signup_funnel_30d} />

      <LatencySpeedometer />

      {user?.role === 'founder' && <LaunchBannerAdminCard />}

      {/* Charts row */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-5">
        <Card title="MRR Growth" subtitle="Last 12 months" testid="chart-mrr" className="lg:col-span-2">
          <div className="p-4 h-72">
            {mrr_chart.length === 0 ? <Empty>No data yet.</Empty> : (
              <ResponsiveContainer width="100%" height="100%">
                <AreaChart data={mrr_chart}>
                  <defs>
                    <linearGradient id="mrrGrad" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="5%" stopColor="#6366f1" stopOpacity={0.4} />
                      <stop offset="95%" stopColor="#6366f1" stopOpacity={0} />
                    </linearGradient>
                  </defs>
                  <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" vertical={false} />
                  <XAxis dataKey="month" tick={{ fontSize: 10, fill: '#64748b' }} />
                  <YAxis tick={{ fontSize: 10, fill: '#64748b' }} tickFormatter={(v) => `₹${(v/1000).toFixed(0)}k`} />
                  <Tooltip formatter={(v) => fmtINR(v)} labelStyle={{ fontSize: 11 }} contentStyle={{ fontSize: 11, border: '1px solid #e2e8f0', borderRadius: 8 }} />
                  <Area type="monotone" dataKey="mrr" stroke="#6366f1" strokeWidth={2} fill="url(#mrrGrad)" />
                </AreaChart>
              </ResponsiveContainer>
            )}
          </div>
        </Card>

        <Card title="Plan Distribution" subtitle="Across active tenants" testid="chart-plans">
          <div className="p-4 h-72">
            <ResponsiveContainer width="100%" height="100%">
              <PieChart>
                <Pie data={plan_distribution} dataKey="count" nameKey="tier" cx="50%" cy="50%" outerRadius={70} label>
                  {plan_distribution.map((p, i) => <Cell key={p.tier || `plan-${i}`} fill={PIE_COLORS[i]} />)}
                </Pie>
                <Tooltip formatter={(v, n) => [v + ' clinics', n]} contentStyle={{ fontSize: 11 }} />
                <Legend wrapperStyle={{ fontSize: 11 }} />
              </PieChart>
            </ResponsiveContainer>
          </div>
        </Card>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-5">
        <Card title="Signups — last 30 days" subtitle="Daily count" testid="chart-signups">
          <div className="p-4 h-56">
            {signups_trend.length === 0 ? <Empty>No signups this window.</Empty> : (
              <ResponsiveContainer width="100%" height="100%">
                <LineChart data={signups_trend}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" vertical={false} />
                  <XAxis dataKey="day" tick={{ fontSize: 10, fill: '#64748b' }} />
                  <YAxis tick={{ fontSize: 10, fill: '#64748b' }} allowDecimals={false} />
                  <Tooltip contentStyle={{ fontSize: 11 }} />
                  <Line type="monotone" dataKey="count" stroke="#10b981" strokeWidth={2} dot={{ r: 3 }} />
                </LineChart>
              </ResponsiveContainer>
            )}
          </div>
        </Card>

        <Card title="Revenue by Tier" subtitle="Monthly equivalent" testid="chart-revenue-tier">
          <div className="p-4 h-56">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={revenue_by_tier}>
                <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" vertical={false} />
                <XAxis dataKey="tier" tick={{ fontSize: 11, fill: '#64748b' }} />
                <YAxis tick={{ fontSize: 10, fill: '#64748b' }} tickFormatter={(v) => `₹${(v/1000).toFixed(0)}k`} />
                <Tooltip formatter={(v) => fmtINR(v)} contentStyle={{ fontSize: 11 }} />
                <Bar dataKey="revenue" fill="#d946ef" radius={[6, 6, 0, 0]} />
              </BarChart>
            </ResponsiveContainer>
          </div>
        </Card>

        <Card title="Conversion Funnel" subtitle="Leads → Trials → Paid" testid="chart-funnel">
          <div className="p-5 space-y-3 min-w-0 overflow-hidden">
            {[
              { label: 'Leads / Waitlist', value: funnel.leads, tone: 'bg-slate-400' },
              { label: 'On Trial', value: funnel.trials, tone: 'bg-indigo-400' },
              { label: 'Paid', value: funnel.paid, tone: 'bg-emerald-500' },
            ].map((r, i, arr) => {
              const max = Math.max(arr[0].value, 1);
              const pct = Math.min(100, Math.max(0, (r.value / max) * 100));
              return (
                <div key={r.label} className="min-w-0">
                  <div className="flex justify-between text-xs mb-1 gap-2">
                    <span className="font-semibold text-slate-700 truncate">{r.label}</span>
                    <span className="text-slate-500 flex-shrink-0">{r.value}</span>
                  </div>
                  <div className="h-5 bg-slate-100 rounded overflow-hidden">
                    <div className={`h-5 rounded ${r.tone} transition-all duration-500`} style={{ width: `${pct}%` }} />
                  </div>
                </div>
              );
            })}
            <div className="pt-1 text-[11px] text-slate-600">
              Trial → Paid: <span className="font-bold text-emerald-700">{funnel.trial_to_paid_pct}%</span>
            </div>
          </div>
        </Card>
      </div>

      {/* Table row */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-5">
        <Card title="Recent Signups" subtitle="Latest 10" testid="tbl-recent-signups">
          <table className="w-full text-sm">
            <thead className="bg-slate-50 text-[10px] uppercase tracking-wider text-slate-500">
              <tr>
                <th className="px-4 py-2 text-left">Clinic</th>
                <th className="px-4 py-2 text-left">City</th>
                <th className="px-4 py-2 text-left">Tier</th>
                <th className="px-4 py-2 text-left">Signed up</th>
              </tr>
            </thead>
            <tbody>
              {recent_signups.map((c) => (
                <tr key={c.clinic_id} className="border-t border-slate-100 hover:bg-slate-50">
                  <td className="px-4 py-2">
                    <Link to={`/admin/tenants/${c.clinic_id}`} className="font-semibold text-indigo-700 hover:underline">{c.name}</Link>
                  </td>
                  <td className="px-4 py-2 text-xs text-slate-500">{c.city || '—'}</td>
                  <td className="px-4 py-2"><Pill tone={tierTone(c.subscription_tier)}>{c.subscription_tier || 'BASIC'}</Pill></td>
                  <td className="px-4 py-2 text-xs text-slate-500">{fmtDate(c.created_at)}</td>
                </tr>
              ))}
              {recent_signups.length === 0 && <tr><td colSpan={4}><Empty>No signups yet.</Empty></td></tr>}
            </tbody>
          </table>
        </Card>

        <Card title="Renewals Due" subtitle="Trials ending in ≤14 days" testid="tbl-renewals">
          <table className="w-full text-sm">
            <thead className="bg-slate-50 text-[10px] uppercase tracking-wider text-slate-500">
              <tr>
                <th className="px-4 py-2 text-left">Clinic</th>
                <th className="px-4 py-2 text-left">Contact</th>
                <th className="px-4 py-2 text-left">Trial ends</th>
              </tr>
            </thead>
            <tbody>
              {renewals_due.map((c) => (
                <tr key={c.clinic_id} className="border-t border-slate-100">
                  <td className="px-4 py-2">
                    <Link to={`/admin/tenants/${c.clinic_id}`} className="font-semibold text-indigo-700 hover:underline">{c.name}</Link>
                    <div className="text-[10px] text-slate-500">{c.city || ''}</div>
                  </td>
                  <td className="px-4 py-2 text-xs">{c.email}<div className="text-[10px] text-slate-500">{c.phone}</div></td>
                  <td className="px-4 py-2 text-xs font-semibold text-amber-700">{fmtDate(c.trial_ends_at)}</td>
                </tr>
              ))}
              {renewals_due.length === 0 && <tr><td colSpan={3}><Empty>No renewals due.</Empty></td></tr>}
            </tbody>
          </table>
        </Card>
      </div>
    </div>
  );
}
