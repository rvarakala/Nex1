/**
 * Settings → Integrations
 *
 * Unified hub that consolidates AUDINEXA's third-party integrations into
 * a single provider-card view. This page does NOT invent new integration
 * capabilities — it surfaces the configuration status of integrations
 * that already exist and are already wired to the backend.
 *
 * Data source: GET /api/settings/integrations
 * (Reuses existing env-var + whatsapp_configs presence-checks; never
 *  exposes secret values.)
 *
 * Actions:
 *   • Configure → deep-links to the existing per-integration Settings
 *     tab where the clinic can actually change something (currently
 *     only WhatsApp / MSG91).
 *   • Platform-managed → text-only status; the integration is wired
 *     via env vars at deployment time and does not have a per-clinic
 *     configuration surface.
 */
import React, { useCallback, useEffect, useState } from 'react';
import axios from 'axios';
import { NavLink } from 'react-router-dom';
import {
  Puzzle, CreditCard, MessageCircle, Mail, MessageSquare,
  CheckCircle2, AlertTriangle, XCircle, HelpCircle, Loader2, RefreshCw,
} from 'lucide-react';
import { useAuth } from '../../AuthContext';
import ErrorToast, { describeError } from '../../components/ErrorToast';

const API = process.env.REACT_APP_BACKEND_URL + '/api';

// Provider-specific icons. Falls back to Puzzle for unknown providers so
// the tab keeps rendering if a new integration is added on the backend.
const PROVIDER_ICONS = {
  razorpay: CreditCard,
  msg91_whatsapp: MessageCircle,
  zeptomail: Mail,
  twilio_sms: MessageSquare,
};

// Status badge colours + copy — mirrors the vocabulary used in
// backend/routers/status_page.py so users see one consistent set of
// words across the Settings hub and the public status page.
const STATUS_META = {
  operational: {
    label: 'Connected',
    tone: 'bg-emerald-50 text-emerald-800 border-emerald-200',
    Icon: CheckCircle2,
  },
  degraded: {
    label: 'Needs attention',
    tone: 'bg-amber-50 text-amber-800 border-amber-200',
    Icon: AlertTriangle,
  },
  outage: {
    label: 'Unreachable',
    tone: 'bg-rose-50 text-rose-800 border-rose-200',
    Icon: XCircle,
  },
  unknown: {
    label: 'Not configured',
    tone: 'bg-slate-50 text-slate-600 border-slate-200',
    Icon: HelpCircle,
  },
  not_available: {
    label: 'Not available on this plan',
    tone: 'bg-slate-50 text-slate-500 border-slate-200',
    Icon: HelpCircle,
  },
};

const CATEGORY_TONE = {
  Payments: 'bg-indigo-50 text-indigo-700',
  Messaging: 'bg-violet-50 text-violet-700',
  Email: 'bg-sky-50 text-sky-700',
  SMS: 'bg-amber-50 text-amber-700',
};

const fmtDateTime = (iso) => {
  if (!iso) return '—';
  try {
    return new Date(iso).toLocaleString('en-IN', {
      dateStyle: 'medium', timeStyle: 'short',
    });
  } catch {
    return iso;
  }
};

export default function SettingsIntegrationsTab() {
  const { user } = useAuth();
  const [payload, setPayload] = useState(null);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState(null);

  const load = useCallback(async () => {
    setLoading(true);
    setErr(null);
    try {
      const { data } = await axios.get(`${API}/settings/integrations`);
      setPayload(data);
    } catch (e) {
      setErr(describeError(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  if (loading && !payload) {
    return (
      <div
        className="flex items-center justify-center py-16 text-slate-500"
        data-testid="integrations-loading"
      >
        <Loader2 size={20} className="animate-spin mr-2" />
        Loading integrations…
      </div>
    );
  }

  const integrations = payload?.integrations || [];

  return (
    <div className="p-4 md:p-6 max-w-5xl" data-testid="integrations-tab">
      {err && <ErrorToast message={err} onClose={() => setErr(null)} />}

      <header className="mb-6" data-testid="integrations-header">
        <div className="flex items-center gap-2 mb-1">
          <Puzzle size={18} className="text-indigo-600" />
          <h1
            className="text-lg font-bold text-slate-900"
            data-testid="integrations-heading"
          >
            Integrations
          </h1>
        </div>
        <p className="text-sm text-slate-600">
          Third-party services powering AUDINEXA — payments, messaging, and
          delivery. Platform-managed integrations are wired at deployment;
          clinic-managed ones can be configured here.
        </p>
        <div className="flex items-center gap-3 mt-3 text-xs text-slate-500">
          <span data-testid="integrations-as-of">
            Snapshot as of {fmtDateTime(payload?.as_of)}
          </span>
          <button
            type="button"
            onClick={load}
            data-testid="integrations-refresh"
            className="inline-flex items-center gap-1 px-2 py-0.5 border border-slate-200 rounded text-slate-600 hover:bg-slate-50"
          >
            <RefreshCw size={12} />
            Refresh
          </button>
        </div>
      </header>

      <div
        className="grid grid-cols-1 md:grid-cols-2 gap-4"
        data-testid="integrations-grid"
      >
        {integrations.map((it) => (
          <IntegrationCard
            key={it.provider_id}
            integration={it}
            currentUserRole={user?.role}
          />
        ))}
      </div>

      <p className="mt-6 text-xs text-slate-500 max-w-3xl">
        This page shows the configuration state of integrations that already
        exist on your AUDINEXA deployment. It never displays secret values.
        Secrets and API keys are stored server-side and are only visible to
        your Emergent deployment.
      </p>
    </div>
  );
}

function IntegrationCard({ integration, currentUserRole }) {
  const {
    provider_id: providerId,
    name,
    category,
    purpose,
    status,
    detail,
    managed_by: managedBy,
    action_href: actionHref,
    action_label: actionLabel,
  } = integration;
  const Icon = PROVIDER_ICONS[providerId] || Puzzle;
  const meta = STATUS_META[status] || STATUS_META.unknown;
  const StatusIcon = meta.Icon;
  const isOwner = ['clinic_owner', 'super_admin', 'founder'].includes(currentUserRole);

  return (
    <article
      className="bg-white rounded-lg border border-slate-200 p-4 flex flex-col gap-3"
      data-testid={`integration-card-${providerId}`}
    >
      <div className="flex items-start justify-between gap-3">
        <div className="flex items-center gap-3 min-w-0">
          <div className="w-10 h-10 rounded-lg bg-indigo-50 border border-indigo-100 flex items-center justify-center shrink-0">
            <Icon size={18} className="text-indigo-600" />
          </div>
          <div className="min-w-0">
            <div
              className="text-sm font-bold text-slate-900 truncate"
              data-testid={`integration-name-${providerId}`}
            >
              {name}
            </div>
            <span
              className={`inline-block text-[10px] uppercase tracking-wider font-semibold px-1.5 py-0.5 rounded ${CATEGORY_TONE[category] || 'bg-slate-100 text-slate-600'}`}
              data-testid={`integration-category-${providerId}`}
            >
              {category}
            </span>
          </div>
        </div>
        <span
          className={`inline-flex items-center gap-1 text-[11px] font-semibold px-2 py-1 rounded border ${meta.tone} shrink-0`}
          data-testid={`integration-status-${providerId}`}
        >
          <StatusIcon size={12} />
          {meta.label}
        </span>
      </div>

      <p
        className="text-xs text-slate-600 leading-relaxed"
        data-testid={`integration-purpose-${providerId}`}
      >
        {purpose}
      </p>

      {detail && (
        <div
          className="text-xs text-slate-500 bg-slate-50 rounded px-2 py-1.5 border border-slate-100"
          data-testid={`integration-detail-${providerId}`}
        >
          {detail}
        </div>
      )}

      <div className="flex items-center justify-between pt-2 border-t border-slate-100">
        <span
          className="text-[10px] uppercase tracking-wider font-semibold text-slate-400"
          data-testid={`integration-managed-by-${providerId}`}
        >
          {managedBy === 'clinic' ? 'Clinic-managed' : 'Platform-managed'}
        </span>
        {actionHref && actionLabel ? (
          <NavLink
            to={actionHref}
            className={`inline-flex items-center gap-1 px-3 py-1 rounded text-xs font-semibold ${
              isOwner
                ? 'bg-indigo-600 text-white hover:bg-indigo-700'
                : 'bg-slate-100 text-slate-400 cursor-not-allowed'
            }`}
            data-testid={`integration-action-${providerId}`}
            onClick={(e) => { if (!isOwner) e.preventDefault(); }}
          >
            {actionLabel}
          </NavLink>
        ) : (
          <span
            className="text-[11px] text-slate-500 italic"
            data-testid={`integration-managed-note-${providerId}`}
          >
            Managed by your deployment
          </span>
        )}
      </div>
    </article>
  );
}
